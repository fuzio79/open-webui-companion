"""Tests for Open-WebUI's flat-argument shape against body models.

The bug class: Open-WebUI's OpenAPI tool layer flattens path and body
parameters into ONE flat JSON object. So the route's own path params
(`owner`, `repo`, `index`) arrive in the request *body* as well.

`IssueUpdate`, `PRUpdate` and `IssueStateChange` used `extra="forbid"`, so the
arguments the tool itself advertised produced

    422 Extra inputs are not permitted [body, owner]

and never reached Gitea. The tool was uncallable from Open-WebUI. Direct HTTP
callers never noticed, because sending only the documented fields works.

`CommentCreate` was the opposite failure: it *required* `owner`, `repo` and
`issue_index` in the body while the route took the issue from the path, so the
body could name a different issue than the one the comment landed on.
"""

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

OPEN_ISSUE = {
    "number": 7,
    "title": "Sprocket misaligned",
    "state": "open",
    "closed_at": None,
    "html_url": "https://gitea.example/LLM/demo/issues/7",
}
CLOSED_ISSUE = dict(OPEN_ISSUE, state="closed", closed_at="2026-11-01T11:51:36+01:00")
PR = {
    "number": 3,
    "title": "Add sprocket",
    "state": "open",
    "merged": False,
    "html_url": "https://gitea.example/LLM/demo/pulls/3",
}


def flat(**extra):
    """The body Open-WebUI actually sends: path params echoed alongside."""
    return {"owner": "LLM", "repo": "demo", "index": 7, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def patches(self, contains="/issues/7"):
        return self.fake.calls_matching("PATCH", contains)


class TestIssueUpdateAcceptsEcho(Base):
    def test_flat_path_params_in_body_are_accepted(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.patch(
            "/gitea/issues/LLM/demo/7", json=flat(title="renamed")
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_echoed_path_params_are_not_forwarded_to_gitea(self):
        """They are tolerated, not laundered into the PATCH payload."""
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.client.patch("/gitea/issues/LLM/demo/7", json=flat(title="renamed"))
        self.assertEqual(self.patches()[0][2], {"title": "renamed"})

    def test_typo_is_still_rejected_despite_the_echo_tolerance(self):
        """The typo guard must survive: this is the whole reason to care."""
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.patch(
            "/gitea/issues/LLM/demo/7", json=flat(state_closed="closed")
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.patches(), [], "no PATCH must be attempted")

    def test_typo_alongside_a_valid_field_is_still_rejected(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.patch(
            "/gitea/issues/LLM/demo/7", json=flat(title="x", state_closed="closed")
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.patches(), [])

    def test_bare_documented_fields_still_work(self):
        """Direct HTTP callers (curl, tests) must not regress."""
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.patch("/gitea/issues/LLM/demo/7", json={"title": "renamed"})
        self.assertEqual(r.status_code, 200, r.text)


class TestPRUpdateAcceptsEcho(Base):
    def test_flat_path_params_in_body_are_accepted(self):
        self.fake.route("GET", "/pulls/3", FakeResponse(200, PR))
        self.fake.route("PATCH", "/pulls/3", FakeResponse(200, PR))
        r = self.client.patch(
            "/gitea/pulls/LLM/demo/3",
            json={"owner": "LLM", "repo": "demo", "index": 3, "title": "t"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.patches("/pulls/3")[0][2], {"title": "t"})

    def test_typo_still_rejected(self):
        r = self.client.patch(
            "/gitea/pulls/LLM/demo/3",
            json={"owner": "LLM", "repo": "demo", "index": 3, "stat": "closed"},
        )
        self.assertEqual(r.status_code, 422, r.text)


class TestCloseIssueAcceptsEcho(Base):
    def test_close_with_flat_args_calls_gitea(self):
        """Was 422 before the fix: the tool could not be called at all."""
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(200, CLOSED_ISSUE))
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/close", json=flat(comment="done")
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "closed")
        self.assertTrue(r.json()["comment_posted"])

    def test_close_typo_still_rejected(self):
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/close", json=flat(commment="typo")
        )
        self.assertEqual(r.status_code, 422, r.text)


class TestPostCommentIdentity(Base):
    """The body must not be able to name a different issue than the path."""

    def test_body_only_needs_the_comment_text(self):
        """`owner`/`repo`/`issue_index` are path-derived, not required."""
        self.fake.route("POST", "/issues/7/comments", FakeResponse(201, {"id": 1}))
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/comments", json={"body": "hello"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        posted = self.fake.calls_matching("POST", "/comments")
        self.assertEqual(posted[0][2], {"body": "hello"})

    def test_flat_args_still_accepted(self):
        self.fake.route("POST", "/issues/7/comments", FakeResponse(201, {"id": 1}))
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/comments", json=flat(issue_index=7, body="hi")
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_mismatched_issue_index_is_rejected_not_redirected(self):
        """Previously: comment went to issue 7 anyway and returned success."""
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/comments",
            json={"issue_index": 5, "body": "hi"},
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(
            self.fake.calls_matching("POST", "/comments"),
            [],
            "must not post to the path issue when the body disagrees",
        )

    def test_matching_issue_index_is_fine(self):
        self.fake.route("POST", "/issues/7/comments", FakeResponse(201, {"id": 1}))
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/comments",
            json={"issue_index": 7, "body": "hi"},
        )
        self.assertEqual(r.status_code, 200, r.text)


class TestReviewAcceptsEcho(Base):
    def test_review_with_flat_args(self):
        self.fake.route("POST", "/pulls/3/reviews", FakeResponse(201, {"id": 1}))
        r = self.client.post(
            "/gitea/pulls/LLM/demo/3/reviews",
            json={
                "owner": "LLM",
                "repo": "demo",
                "index": 3,
                "body": "lgtm",
                "type": "approved",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.fake.calls_matching("POST", "/reviews")[0][2]
        # Gitea >= 1.24 / Forgejo read `event` (uppercase); <= 1.23 read the
        # legacy `type` (lowercase). Both are sent so either generation works.
        # Sending only type="approved" silently created a PENDING review that
        # was never posted -- that was the bug, so this pins the fix.
        self.assertEqual(sent, {"event": "APPROVED", "type": "approve", "body": "lgtm"})
        # The echoed path params must NOT be laundered into the payload.
        for leaked in ("owner", "repo", "index"):
            self.assertNotIn(leaked, sent)


class TestOpenAPIDoesNotAdvertiseUnusableFields(unittest.TestCase):
    """The spec Open-WebUI builds its tool schema from."""

    @classmethod
    def setUpClass(cls):
        spec = TestClient(app).get("/openapi.json").json()
        cls.spec = spec

        def body_fields(path, method):
            op = spec["paths"][path][method]
            ref = op["requestBody"]["content"]["application/json"]["schema"]
            # A body declared as `Model | None` is published as
            # {"anyOf": [{"$ref": Model}, {"type": "null"}]} -- unwrap it.
            if "anyOf" in ref:
                ref = next(
                    (b for b in ref["anyOf"] if b.get("$ref")),
                    {},
                )
            name = ref.get("$ref", "").split("/")[-1]
            sch = spec["components"]["schemas"].get(name, {})
            return set(sch.get("properties", {})), set(sch.get("required") or [])

        cls.body_fields = staticmethod(body_fields)

    def test_comment_body_does_not_require_identity_fields(self):
        props, required = self.body_fields(
            "/gitea/issues/{owner}/{repo}/{index}/comments", "post"
        )
        self.assertEqual(required, {"body"})
        self.assertIn("owner", props, "still advertised, so the echo validates")

    def test_close_issue_advertises_the_echo_fields(self):
        props, _ = self.body_fields(
            "/gitea/issues/{owner}/{repo}/{index}/close", "post"
        )
        for f in ("owner", "repo", "index"):
            self.assertIn(f, props, f"{f} must be accepted from Open-WebUI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
