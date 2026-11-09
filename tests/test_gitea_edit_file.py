"""Tests for GiteaEditFile.

The tool exists because hand-written diffs fail in ways that are hard to see:
line numbers, context lines, and end-of-file newline handling. So the tests
assert the GENERATED DIFF, not just the resulting text -- a diff that produces
the right text locally can still be rejected by Gitea ('corrupt patch'), and
that is exactly what happened during development.
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

from app.gitea import _build_file_diff, _diff_stats
from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

COMMIT_SHA = "d" * 40
MARKER = "No newline at end of file"


def contents(text, sha="1" * 40, size=None):
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
        # NOTE: no default-branch route by default. The fake matches on
        # substrings and returns the FIRST match, so a route for
        # /repos/LLM/demo would also swallow /repos/LLM/demo/contents/f.txt
        # and hand back a repo payload where file contents were expected.
        # Tests that need the default branch register it explicitly.
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def file(self, path, old, new):
        return {"path": path, "edits": [{"old_text": old, "new_text": new}]}

    def edit(self, files, **overrides):
        body = {"files": files, "message": "edit", "branch": "main"}
        body.update(overrides)
        return self.client.post("/gitea/edits/LLM/demo", json=body)

    def posts(self):
        return self.fake.calls_matching("POST", "diffpatch")

    def sent_diff(self):
        return self.posts()[0][2]["content"]

    def reason(self, response):
        return response.json()["detail"]["blocked_reason"]


class TestEditHappyPath(Base):
    def test_replaces_text_and_reports_commit(self):
        self.fake.route("GET", "/contents/f.txt", contents("one\ntwo\nthree\n"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.edit([self.file("f.txt", "two", "TWO")])
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(COMMIT_SHA, data["commit_sha"])
        self.assertEqual(["f.txt"], data["files_changed"])
        self.assertEqual(1, data["results"][0]["edits_applied"])
        self.assertEqual("f.txt", data["results"][0]["path"])

    def test_sends_a_diff_not_the_whole_file(self):
        self.fake.route("GET", "/contents/f.txt", contents("one\ntwo\nthree\n"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        self.edit([self.file("f.txt", "two", "TWO")])
        diff = self.sent_diff()
        self.assertIn("@@", diff)
        self.assertIn("-two", diff)
        self.assertIn("+TWO", diff)
        self.assertNotIn("+one", diff)

    def test_applies_edits_in_order_so_a_later_edit_sees_earlier_output(self):
        self.fake.route("GET", "/contents/f.txt", contents("alpha\n"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.edit([{
            "path": "f.txt",
            "edits": [
                {"old_text": "alpha", "new_text": "beta"},
                {"old_text": "beta", "new_text": "gamma"},
            ],
        }])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2, response.json()["results"][0]["edits_applied"])
        self.assertIn("+gamma", self.sent_diff())

    def test_multiple_files_land_in_one_diff_and_one_commit(self):
        self.fake.route("GET", "/contents/a.txt", contents("A\n"))
        self.fake.route("GET", "/contents/b.txt", contents("B\n"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.edit([
            self.file("a.txt", "A", "A2"),
            self.file("b.txt", "B", "B2"),
        ])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(self.posts()))
        diff = self.sent_diff()
        self.assertIn("a/a.txt", diff)
        self.assertIn("a/b.txt", diff)
        self.assertEqual(["a.txt", "b.txt"], response.json()["files_changed"])
        self.assertEqual(2, len(response.json()["results"]))

    def test_no_trailing_newline_file_is_preserved(self):
        self.fake.route("GET", "/contents/f.txt", contents("a\nb\nc"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.edit([self.file("f.txt", "b", "B")])
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn(MARKER, self.sent_diff())

    def test_deletion_is_an_empty_new_text(self):
        self.fake.route("GET", "/contents/f.txt", contents("keep\nremove me\n"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.edit([self.file("f.txt", "remove me\n", "")])
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("-remove me", self.sent_diff())

    def test_branch_is_forwarded_to_the_write(self):
        self.fake.route("GET", "/contents/f.txt", contents("a\n"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        self.edit([self.file("f.txt", "a", "b")], branch="feature/x")
        self.assertEqual("feature/x", self.posts()[0][2]["branch"])


class TestEditRefusals(Base):
    """Every refusal must also prove that nothing was written."""

    def assert_refused(self, response, reason, status=422):
        self.assertEqual(status, response.status_code, response.text)
        self.assertEqual(reason, self.reason(response))
        self.assertEqual([], self.posts(), "a refusal must not write")

    def test_old_text_not_found(self):
        self.fake.route("GET", "/contents/f.txt", contents("one\ntwo\n"))
        self.assert_refused(self.edit([self.file("f.txt", "absent", "x")]),
                            "old_text_not_found")

    def test_old_text_ambiguous_is_refused_not_guessed(self):
        self.fake.route("GET", "/contents/f.txt", contents("dup\ndup\n"))
        response = self.edit([self.file("f.txt", "dup", "x")])
        self.assert_refused(response, "old_text_ambiguous")
        self.assertEqual(2, response.json()["detail"]["matches"])

    def test_identical_old_and_new_text_is_not_committed(self):
        self.fake.route("GET", "/contents/f.txt", contents("same\n"))
        self.assert_refused(self.edit([self.file("f.txt", "same", "same")]),
                            "no_change")

    def test_empty_old_text(self):
        self.fake.route("GET", "/contents/f.txt", contents("x\n"))
        self.assert_refused(self.edit([self.file("f.txt", "", "y")]),
                            "empty_old_text")

    def test_missing_file(self):
        self.fake.route("GET", "/contents/ghost.txt",
                        FakeResponse(404, None, text="{\"message\":\"not found\"}"))
        self.assert_refused(self.edit([self.file("ghost.txt", "a", "b")]),
                            "file_not_found", status=422)

    def test_directory_is_not_editable(self):
        self.fake.route("GET", "/contents/dir",
                        FakeResponse(200, [{"name": "x", "type": "file"}]))
        self.assert_refused(self.edit([self.file("dir", "a", "b")]),
                            "file_not_found")

    def test_binary_file(self):
        raw = bytes(range(256))
        self.fake.route("GET", "/contents/bin.dat", contents(raw))
        self.assert_refused(self.edit([self.file("bin.dat", "a", "b")]),
                            "binary_file")

    def test_file_too_large(self):
        self.fake.route("GET", "/contents/big.txt", contents("a\n", size=10 ** 9))
        self.assert_refused(self.edit([self.file("big.txt", "a", "b")]),
                            "file_too_large")

    def test_same_path_listed_twice(self):
        self.fake.route("GET", "/contents/f.txt", contents("a\n"))
        self.assert_refused(
            self.edit([self.file("f.txt", "a", "b"), self.file("f.txt", "b", "c")]),
            "no_change",
        )

    def test_no_files(self):
        self.assert_refused(self.edit([]), "empty_diff")

    def test_new_branch_equal_to_base_is_refused(self):
        self.fake.route("GET", "/contents/f.txt", contents("a\n"))
        self.assert_refused(
            self.edit([self.file("f.txt", "a", "b")], new_branch="main"),
            "base_branch_echo",
        )

    def test_existing_new_branch_is_refused(self):
        self.fake.route_first("GET", "/branches/taken", FakeResponse(200, {"name": "taken"}))
        self.fake.route("GET", "/contents/f.txt", contents("a\n"))
        self.assert_refused(
            self.edit([self.file("f.txt", "a", "b")], new_branch="taken"),
            "new_branch_exists",
            status=409,
        )


class TestEditDiffIsAcceptedUpstream(Base):
    """A generated diff must not be rejected for shape reasons.

    These pin the two failures seen while probing live Gitea: an unterminated
    final line, and a no-newline marker. Both produced a diff that looked
    correct but was answered with 500.
    """

    def captured_diff(self, text, old, new):
        self.fake.route("GET", "/contents/f.txt", contents(text))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.edit([self.file("f.txt", old, new)])
        self.assertEqual(200, response.status_code, response.text)
        return self.sent_diff()

    def test_diff_ends_with_a_newline(self):
        self.assertTrue(self.captured_diff("a\nb\nc", "b", "B").endswith("\n"))

    def test_no_marker_in_diff(self):
        self.assertNotIn(MARKER, self.captured_diff("a\nb\nc", "b", "B"))

    def test_crlf_content_keeps_its_line_endings(self):
        diff = self.captured_diff("a\r\nb\r\nc\r\n", "b", "B")
        self.assertIn("\r\n", diff)

    def test_metadata_unchanged_by_an_edit(self):
        # Every line is terminated, so difflib can never glue the next line
        # onto its final output line ('-c+c'), which Gitea calls 'corrupt'.
        diff = self.captured_diff("a\nb\nc", "b", "B")
        self.assertNotIn("c\n+c", diff.replace(" c\n", " c\n"))
        for line in diff.splitlines():
            self.assertFalse(line.startswith("diff --git") and line.endswith("+"), line)


class TestEditOpenWebUICompatibility(Base):
    def test_flat_path_params_are_tolerated(self):
        self.fake.route("GET", "/contents/f.txt", contents("a\n"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.edit([self.file("f.txt", "a", "b")], owner="LLM", repo="demo")
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn("owner", self.posts()[0][2])

    def test_operation_id_is_published(self):
        spec = self.client.get("/openapi.json").json()
        op = spec["paths"]["/gitea/edits/{owner}/{repo}"]["post"]
        self.assertEqual("GiteaEditFile", op["operationId"])
        ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        name = ref.rsplit("/", 1)[-1]
        schema = self.client.get("/openapi.json").json()["components"]["schemas"][name]
        self.assertIn("files", schema["properties"])


if __name__ == "__main__":
    unittest.main()


class TestCancellingEdits(Base):
    """Edits that cancel out must not produce an empty commit.

    ``a``->``b`` then ``b``->``a`` leaves the file byte-identical. Gitea answers
    an empty diff with 500, and a no-op commit would be the ``PATCH {}`` class of
    bug, so this is refused before anything is sent.
    """

    def test_cancelling_edits_are_refused(self):
        self.fake.route("GET", "/contents/f.txt", contents("a\n"))
        response = self.edit([{
            "path": "f.txt",
            "edits": [
                {"old_text": "a", "new_text": "b"},
                {"old_text": "b", "new_text": "a"},
            ],
        }])
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("no_change", self.reason(response))
        self.assertEqual([], self.posts())
