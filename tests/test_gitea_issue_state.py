"""Tests for Gitea issue close/reopen support.

Standard library only (no pytest, no respx, no network):

    python3 -m unittest discover -s tests -t . -v

httpx.AsyncClient is replaced by tests.fake_gitea.FakeGitea so the FastAPI app
runs in-process and every outbound Gitea call is recorded and asserted on.
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


class TestSilentNoOpRegression(Base):
    """The bug this change exists to fix: a discarded field looked like success."""

    def test_state_is_forwarded_not_dropped(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(201, CLOSED_ISSUE))
        r = self.client.patch("/gitea/issues/LLM/demo/7", json={"state": "closed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "closed")
        self.assertEqual(len(self.patches()), 1)
        self.assertEqual(self.patches()[0][2], {"state": "closed"})

    def test_unknown_field_is_rejected_not_silently_ignored(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.patch(
            "/gitea/issues/LLM/demo/7", json={"state_closed": "closed"}
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.patches(), [], "no PATCH must be attempted")

    def test_unknown_field_alongside_valid_field_is_rejected(self):
        """Isolates extra="forbid": the typo must not ride along silently.

        Without it, {"title": ..., "state_closed": ...} would PATCH just the
        title, succeed, and leave the issue open while the caller believed it
        closed. The empty-payload guard alone cannot catch this.
        """
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.patch(
            "/gitea/issues/LLM/demo/7",
            json={"title": "renamed", "state_closed": "closed"},
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.patches(), [], "no partial PATCH must be sent")

    def test_empty_update_is_rejected(self):
        r = self.client.patch("/gitea/issues/LLM/demo/7", json={})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.patches(), [])

    def test_invalid_state_value_is_rejected(self):
        r = self.client.patch("/gitea/issues/LLM/demo/7", json={"state": "archived"})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.patches(), [])


class TestCloseIssue(Base):
    def test_close_sets_state_and_reports_result(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(201, CLOSED_ISSUE))
        r = self.client.post("/gitea/issues/LLM/demo/7/close")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["state"], "closed")
        self.assertEqual(body["closed_at"], "2026-11-01T11:51:36+01:00")
        self.assertFalse(body["already_in_state"])
        self.assertFalse(body["comment_posted"])
        self.assertEqual(self.patches()[0][2], {"state": "closed"})

    def test_close_without_body_is_allowed(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(201, CLOSED_ISSUE))
        r = self.client.post("/gitea/issues/LLM/demo/7/close", json={})
        self.assertEqual(r.status_code, 200, r.text)

    def test_comment_is_posted_before_state_flip(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route("POST", "/issues/7/comments", FakeResponse(201, {"id": 1}))
        self.fake.route("PATCH", "/issues/7", FakeResponse(201, CLOSED_ISSUE))
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/close", json={"comment": "Done in 1.1.0"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["comment_posted"])
        writes = [c for c in self.fake.calls if c[0] in ("POST", "PATCH")]
        self.assertEqual(writes[0][0], "POST", "comment must come first")
        self.assertIn("/comments", writes[0][1])
        self.assertEqual(writes[-1][0], "PATCH", "state flip must come last")
        self.assertEqual(
            self.fake.calls_matching("POST", "/comments")[0][2],
            {"body": "Done in 1.1.0"},
        )

    def test_comment_failure_aborts_close(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route(
            "POST", "/issues/7/comments", FakeResponse(500, {"message": "boom"})
        )
        r = self.client.post("/gitea/issues/LLM/demo/7/close", json={"comment": "bye"})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(self.patches(), [], "must not close if the comment failed")

    def test_already_closed_is_idempotent_and_skips_comment(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, CLOSED_ISSUE))
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/close", json={"comment": "again"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["already_in_state"])
        self.assertFalse(r.json()["comment_posted"])
        self.assertEqual(self.patches(), [], "no PATCH needed")
        self.assertEqual(
            self.fake.calls_matching("POST", "/comments"), [],
            "comment must not be re-posted",
        )
        self.assertEqual(r.json()["closed_at"], "2026-11-01T11:51:36+01:00")

    def test_missing_issue_returns_404(self):
        self.fake.route("GET", "/issues/7", FakeResponse(404, {"message": "nope"}))
        r = self.client.post("/gitea/issues/LLM/demo/7/close")
        self.assertEqual(r.status_code, 404, r.text)

    def test_unknown_field_on_close_is_rejected(self):
        r = self.client.post(
            "/gitea/issues/LLM/demo/7/close", json={"commment": "typo"}
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_gitea_412_is_remapped_to_422(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        self.fake.route(
            "PATCH", "/issues/7", FakeResponse(412, {"message": "unknown state"})
        )
        r = self.client.post("/gitea/issues/LLM/demo/7/close")
        self.assertEqual(r.status_code, 422, r.text)


class TestReopenIssue(Base):
    def test_reopen_sets_open_and_clears_closed_at(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, CLOSED_ISSUE))
        self.fake.route("PATCH", "/issues/7", FakeResponse(201, OPEN_ISSUE))
        r = self.client.post("/gitea/issues/LLM/demo/7/reopen")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "open")
        self.assertIsNone(r.json()["closed_at"])
        self.assertEqual(self.patches()[0][2], {"state": "open"})

    def test_reopen_already_open_is_idempotent(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.post("/gitea/issues/LLM/demo/7/reopen")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["already_in_state"])
        self.assertEqual(self.patches(), [])


class TestGetIssue(Base):
    def test_get_single_issue(self):
        self.fake.route("GET", "/issues/7", FakeResponse(200, OPEN_ISSUE))
        r = self.client.get("/gitea/issues/LLM/demo/7")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "open")

    def test_list_issues_still_routes_to_collection(self):
        self.fake.route("GET", "/issues", FakeResponse(200, [OPEN_ISSUE]))
        r = self.client.get("/gitea/issues/LLM/demo")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsInstance(r.json(), list)


PR = {
    "number": 3,
    "title": "Add sprocket",
    "state": "open",
    "merged": False,
    "html_url": "https://gitea.example/LLM/demo/pulls/3",
}


class TestClosePR(Base):
    def test_close_pr_without_merging(self):
        self.fake.route("GET", "/pulls/3", FakeResponse(200, PR))
        self.fake.route("PATCH", "/pulls/3", FakeResponse(201, dict(PR, state="closed")))
        r = self.client.post("/gitea/pulls/LLM/demo/3/close")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "closed")
        self.assertFalse(r.json()["merged"])
        self.assertEqual(self.patches("/pulls/3")[0][2], {"state": "closed"})

    def test_merged_pr_cannot_be_closed(self):
        self.fake.route(
            "GET", "/pulls/3", FakeResponse(200, dict(PR, merged=True, state="closed"))
        )
        r = self.client.post("/gitea/pulls/LLM/demo/3/close")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.patches("/pulls/3"), [])

    def test_already_closed_pr_is_idempotent(self):
        self.fake.route("GET", "/pulls/3", FakeResponse(200, dict(PR, state="closed")))
        r = self.client.post("/gitea/pulls/LLM/demo/3/close")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["already_in_state"])
        self.assertEqual(self.patches("/pulls/3"), [])


class TestUpdatePR(Base):
    def test_pr_state_forwarded(self):
        self.fake.route("GET", "/pulls/3", FakeResponse(200, PR))
        self.fake.route("PATCH", "/pulls/3", FakeResponse(201, dict(PR, state="closed")))
        r = self.client.patch("/gitea/pulls/LLM/demo/3", json={"state": "closed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.patches("/pulls/3")[0][2], {"state": "closed"})

    def test_pr_empty_update_rejected(self):
        r = self.client.patch("/gitea/pulls/LLM/demo/3", json={})
        self.assertEqual(r.status_code, 422, r.text)


class TestOpenAPISurface(unittest.TestCase):
    """Guards the tool surface Open-WebUI actually consumes."""

    @classmethod
    def setUpClass(cls):
        cls.spec = TestClient(app).get("/openapi.json").json()
        cls.ops = [
            op.get("operationId")
            for path in cls.spec["paths"].values()
            for op in path.values()
        ]

    def test_new_tools_are_published(self):
        for op in (
            "GiteaCloseIssue",
            "GiteaReopenIssue",
            "GiteaGetIssue",
            "GiteaClosePR",
        ):
            self.assertIn(op, self.ops)

    def test_operation_ids_are_unique(self):
        self.assertEqual(len(self.ops), len(set(self.ops)), "duplicate operationId")

    def test_existing_tools_are_not_dropped(self):
        for op in (
            "GiteaCreateIssue",
            "GiteaListIssues",
            "GiteaUpdateIssue",
            "GiteaPostComment",
            "GiteaListComments",
            "GiteaDeleteComment",
        ):
            self.assertIn(op, self.ops)


if __name__ == "__main__":
    unittest.main(verbosity=2)
