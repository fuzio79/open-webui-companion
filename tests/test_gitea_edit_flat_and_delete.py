"""Tests for the two GiteaEditFile defects found by measurement (2026-11-09).

Both come from the same field report -- "GiteaEditFile dropped `path` from the
files[] object on a multi-line new_text" -- and neither defect was what the
report said it was. Measuring all 63 persisted calls showed:

  * `path` was OMITTED BY THE MODEL on 15 calls (all 15 failed), never lost by
    the transport. Adjacent retries differed by exactly the 21 characters of
    `"path": "STATUS.md", `, and a Pydantic error cannot encode field order, so
    "put path first" was a coincidence, not a mechanism.

So the fixes are not "send path first". They are:

  1. Accept a FLAT single-file body (top-level `path` + `edits`) as well as
     `files[]`. Models are markedly more reliable on a flat object than on
     files -> edits -> old_text/new_text, which is the shape they were failing
     to emit. The nesting is not wrong; it is just harder to write correctly.

  2. `new_text` must not be OPTIONAL. It had `default=""`, which OpenAPI
     publishes as `required=["old_text"]` only -- so the model was told it could
     omit the field, and omission meant "". That is a delete. A hallucinated
     key omission was therefore a silent, unrecoverable edit rather than a
     refusal. Deleting stays legal, but only as an explicit empty string.

These tests pin the PROPERTIES, not the spellings: that the two forms produce
the same commit, that ambiguity is refused rather than resolved, that an
omitted new_text can no longer delete, and that the published OpenAPI schema
tells the model the truth.
"""

import base64
import os
import pathlib
import sys
import unittest

os.environ.setdefault("GITEA_TOKEN", "test-token")
os.environ.setdefault("GITEA_INSTANCE_URL", "https://gitea.example")
os.environ.setdefault("EVIDENCE_DIR", "/tmp/owc-test-data")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

COMMIT_SHA = "e" * 40


def contents(text, sha="2" * 40, size=None):
    raw = text.encode() if isinstance(text, str) else text
    return FakeResponse(200, {
        "sha": sha,
        "size": len(raw) if size is None else size,
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
    })


def commit_payload(sha=COMMIT_SHA):
    return {"content": None, "commit": {"sha": sha, "html_url": "https://gitea.example/c" + sha}}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)
        # The fake returns the FIRST matching route, so a route for the repo
        # itself would swallow /contents/... -- it is registered only where a
        # test needs the default-branch lookup.
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def post(self, body):
        return self.client.post("/gitea/edits/LLM/demo", json=body)

    def posts(self):
        return self.fake.calls_matching("POST", "diffpatch")

    def sent_diff(self):
        return self.posts()[0][2]["content"]

    def reason(self, response):
        return response.json()["detail"]["blocked_reason"]


class TestFlatSingleFileForm(Base):
    """`path`+`edits` at the top level is an alias for a one-entry `files`."""

    def test_flat_form_edits_the_file(self):
        self.fake.route("GET", "/contents/f.txt", contents("one\ntwo\nthree\n"))
        r = self.post({
            "path": "f.txt",
            "edits": [{"old_text": "two", "new_text": "TWO"}],
            "branch": "main",
        })
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(COMMIT_SHA, r.json()["commit_sha"])
        self.assertEqual(["f.txt"], r.json()["files_changed"])
        self.assertIn("+TWO", self.sent_diff())

    def test_flat_and_nested_forms_produce_the_same_diff(self):
        """Not just "both succeed" -- the SAME edit must come out."""

        def diff_from(body):
            # A fresh fake per call, so neither request can be answered by the
            # other's recorded route. setUp() is NOT reused: it drops the
            # contents route registered below.
            self.setUp()
            self.fake.route("GET", "/contents/f.txt", contents("a\nb\nc\n"))
            response = self.post(body)
            self.assertEqual(200, response.status_code, response.text)
            return self.sent_diff()

        flat_diff = diff_from(
            {
                "path": "f.txt",
                "edits": [{"old_text": "b", "new_text": "B"}],
                "branch": "main",
            }
        )
        nested_diff = diff_from(
            {
                "files": [
                    {"path": "f.txt", "edits": [{"old_text": "b", "new_text": "B"}]}
                ],
                "branch": "main",
            }
        )
        self.assertEqual(flat_diff, nested_diff)
        self.assertIn("+B", flat_diff)

    def test_flat_form_applies_multiple_edits_in_order(self):
        self.fake.route("GET", "/contents/f.txt", contents("alpha\n"))
        r = self.post({
            "path": "f.txt",
            "edits": [
                {"old_text": "alpha", "new_text": "beta"},
                {"old_text": "beta", "new_text": "gamma"},
            ],
            "branch": "main",
        })
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(2, r.json()["results"][0]["edits_applied"])
        self.assertIn("+gamma", self.sent_diff())

    def test_flat_form_still_reports_which_file_it_touched(self):
        """The response must name the path, not echo an empty string back."""
        self.fake.route("GET", "/contents/dir/f.txt", contents("x\n"))
        r = self.post({
            "path": "dir/f.txt",
            "edits": [{"old_text": "x", "new_text": "y"}],
            "branch": "main",
        })
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("dir/f.txt", r.json()["results"][0]["path"])


class TestAmbiguityIsRefused(Base):
    """Exactly one form per call. Guessing is how the wrong file gets written."""

    def test_both_forms_together_is_refused_and_writes_nothing(self):
        self.fake.route("GET", "/contents/f.txt", contents("a\n"))
        r = self.post({
            "files": [{"path": "f.txt", "edits": [{"old_text": "a", "new_text": "b"}]}],
            "path": "other.txt",
            "edits": [{"old_text": "a", "new_text": "c"}],
            "branch": "main",
        })
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("ambiguous_request", self.reason(r))
        self.assertEqual([], self.posts(), "an ambiguous request must not write")
        # It must not even READ: naming which file it read would be a guess.
        self.assertEqual([], self.fake.calls_matching("GET", "/contents/"))

    def test_edits_without_path_is_refused_with_the_missing_field_named(self):
        r = self.post({"edits": [{"old_text": "a", "new_text": "b"}], "branch": "main"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("empty_diff", self.reason(r))
        self.assertIn("path", r.json()["detail"]["detail"])
        self.assertEqual([], self.posts())

    def test_path_without_edits_is_refused_not_treated_as_noop(self):
        r = self.post({"path": "f.txt", "branch": "main"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("empty_diff", self.reason(r))
        self.assertIn("f.txt", r.json()["detail"]["detail"])
        self.assertEqual([], self.posts())

    def test_empty_edits_list_with_path_is_refused(self):
        r = self.post({"path": "f.txt", "edits": [], "branch": "main"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("empty_diff", self.reason(r))
        self.assertEqual([], self.posts())

    def test_no_form_at_all_is_still_refused(self):
        r = self.post({"branch": "main"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("empty_diff", self.reason(r))
        self.assertEqual([], self.posts())


class TestOmittingNewTextCannotDelete(Base):
    """The data-loss path: `default=""` made omission mean deletion.

    Deleting must remain possible -- it is a legitimate edit -- but it must be
    something the caller SAID, not something it forgot.
    """

    def test_omitted_new_text_is_refused_before_anything_is_read_or_written(self):
        r = self.post({
            "files": [{"path": "f.txt", "edits": [{"old_text": "keep me\n"}]}],
            "branch": "main",
        })
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual([], self.posts(), "omission must never delete")
        self.assertEqual([], self.fake.calls_matching("GET", "/contents/"))
        # The error must name the FIELD, so a model can correct itself rather
        # than guess. Raw Pydantic text is what made it guess "reorder keys".
        self.assertIn("new_text", r.text)

    def test_omitted_new_text_is_refused_in_the_flat_form_too(self):
        r = self.post({
            "path": "f.txt",
            "edits": [{"old_text": "keep me\n"}],
            "branch": "main",
        })
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual([], self.posts())
        self.assertIn("new_text", r.text)

    def test_explicit_empty_string_still_deletes(self):
        """The deliberate path must keep working -- this is the anti-vacuity test."""
        self.fake.route("GET", "/contents/f.txt", contents("keep\nremove me\n"))
        r = self.post({
            "files": [{"path": "f.txt", "edits": [{"old_text": "remove me\n", "new_text": ""}]}],
            "branch": "main",
        })
        self.assertEqual(200, r.status_code, r.text)
        self.assertIn("-remove me", self.sent_diff())


class TestPublishedSchemaTellsTheModelTheTruth(unittest.TestCase):
    """The spec Open-WebUI turns into the model-facing tool schema.

    This is the layer that decided the bug: a field with a default is
    advertised as OPTIONAL, so the model is told it may omit it. That is
    exactly how `new_text` came to mean "delete".
    """

    @classmethod
    def setUpClass(cls):
        spec = TestClient(app).get("/openapi.json").json()
        cls.spec = spec

    def schema(self, name):
        return self.spec["components"]["schemas"][name]

    def test_new_text_is_required(self):
        required = self.schema("TextEdit").get("required") or []
        self.assertIn(
            "new_text",
            required,
            "new_text must be REQUIRED: an optional new_text is a schema that "
            "tells the model 'you may omit this', and omitting it deletes text",
        )
        self.assertIn("old_text", required)

    def test_new_text_no_longer_carries_a_default(self):
        """A default is what makes it optional; assert the cause, not the symptom."""
        prop = self.schema("TextEdit")["properties"]["new_text"]
        self.assertNotIn("default", prop)

    def test_path_is_still_required_inside_files(self):
        required = self.schema("FileEditSpec").get("required") or []
        self.assertIn("path", required)
        self.assertIn("edits", required)

    def test_both_forms_are_advertised_on_the_request(self):
        props = self.schema("FileEditRequest")["properties"]
        for field in ("files", "path", "edits"):
            self.assertIn(field, props, f"{field} must be visible to the model")

    def test_neither_form_is_globally_required(self):
        """Requiring `files` would make the flat form unusable and vice versa."""
        required = self.schema("FileEditRequest").get("required") or []
        self.assertNotIn("files", required)
        self.assertNotIn("path", required)

    def test_operation_is_still_callable_under_its_published_name(self):
        op = self.spec["paths"]["/gitea/edits/{owner}/{repo}"]["post"]
        self.assertEqual("GiteaEditFile", op["operationId"])


if __name__ == "__main__":
    unittest.main()
