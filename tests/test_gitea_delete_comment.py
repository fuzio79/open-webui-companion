"""Tests for Gitea comment deletion and empty-body handling.

Standard library only (no pytest, no respx, no network):

    python3 -m unittest discover -s tests -t . -v

The bug these tests exist for: Gitea answers a successful comment delete with
``204 No Content``. ``_gitea_request`` called ``response.json()`` on that empty
body, which raised ``JSONDecodeError`` — not an ``HTTPStatusError`` and not a
``RequestError``, so it escaped both handlers and surfaced as a 500 *after* the
comment had already been deleted. A caller receiving 500 concludes the delete
failed, retries, or reports a false error.
"""

import asyncio
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

from app.gitea import _gitea_request
from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

# What Gitea actually returns for a successful delete: no body at all.
NO_CONTENT = FakeResponse(204, None, text="")


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def deletes(self):
        return self.fake.calls_matching("DELETE", "/issues/comments/")


class TestDeleteCommentSucceeds(Base):
    def test_204_is_reported_as_success_not_500(self):
        """The regression: a real, successful delete must not raise a 500."""
        self.fake.route("DELETE", "/issues/comments/124", NO_CONTENT)
        r = self.client.delete("/gitea/issues/LLM/demo/1/comments/124")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "deleted")
        self.assertEqual(r.json()["comment_id"], 124)

    def test_delete_targets_repo_scoped_endpoint(self):
        """No issue number in the upstream URL — Gitea's is repo-scoped."""
        self.fake.route("DELETE", "/issues/comments/124", NO_CONTENT)
        self.client.delete("/gitea/issues/LLM/demo/1/comments/124")
        calls = self.deletes()
        self.assertEqual(len(calls), 1)
        url = calls[0][1]
        self.assertIn("/repos/LLM/demo/issues/comments/124", url)
        self.assertNotIn("/issues/1/", url)

    def test_index_is_ignored_and_documented_in_response(self):
        """A wrong index must not change or fail the call, and must be disclosed."""
        self.fake.route("DELETE", "/issues/comments/124", NO_CONTENT)
        right = self.client.delete("/gitea/issues/LLM/demo/1/comments/124")
        wrong = self.client.delete("/gitea/issues/LLM/demo/999/comments/124")
        self.assertEqual(right.status_code, 200, right.text)
        self.assertEqual(wrong.status_code, 200, wrong.text)
        self.assertEqual(wrong.json()["index_ignored"], 999)
        # Same upstream target either way — proving index has no effect.
        urls = {c[1] for c in self.deletes()}
        self.assertEqual(len(urls), 1, urls)

    def test_missing_comment_returns_404_not_500(self):
        self.fake.route(
            "DELETE",
            "/issues/comments/999999",
            FakeResponse(
                404, {"message": "comment does not exist [id: 999999]"}
            ),
        )
        r = self.client.delete("/gitea/issues/LLM/demo/1/comments/999999")
        self.assertEqual(r.status_code, 404, r.text)

    def test_upstream_error_is_not_reported_as_success(self):
        self.fake.route(
            "DELETE", "/issues/comments/124", FakeResponse(403, {"message": "nope"})
        )
        r = self.client.delete("/gitea/issues/LLM/demo/1/comments/124")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertNotIn("deleted", r.text)


class TestRequestBodyHandling(Base):
    """_gitea_request must survive 204/empty/non-JSON bodies generally."""

    def call(self, method="DELETE", url="https://gitea.example/api/v1/x"):
        return asyncio.run(_gitea_request(method, url))

    def test_204_returns_empty_dict(self):
        self.fake.route("DELETE", "/api/v1/x", NO_CONTENT)
        self.assertEqual(self.call(), {})

    def test_200_with_empty_body_returns_empty_dict(self):
        self.fake.route("GET", "/api/v1/x", FakeResponse(200, None, text=""))
        self.assertEqual(self.call("GET"), {})

    def test_json_body_is_returned_unchanged(self):
        payload = {"id": 1, "state": "open"}
        self.fake.route("GET", "/api/v1/x", FakeResponse(200, payload))
        self.assertEqual(self.call("GET"), payload)

    def test_non_json_body_is_returned_as_text(self):
        diff = "diff --git a/f b/f\n+added\n"
        self.fake.route("GET", "/api/v1/x", FakeResponse(200, None, text=diff))
        self.assertEqual(self.call("GET"), diff)

    def test_no_500_on_204_through_a_route_that_decodes_json(self):
        """Guards future endpoints, not just delete_comment."""
        self.fake.route("GET", "/user/info", NO_CONTENT)
        r = self.client.get("/gitea/health")
        self.assertEqual(r.status_code, 200, r.text)


class TestOpenAPISurface(unittest.TestCase):
    """Open-WebUI consumes this spec, so the tool must not disappear."""

    @classmethod
    def setUpClass(cls):
        spec = TestClient(app).get("/openapi.json").json()
        cls.delete_path = "/gitea/issues/{owner}/{repo}/{index}/comments/{comment_id}"
        cls.op = spec["paths"][cls.delete_path]["delete"]

    def test_operation_id_is_published(self):
        self.assertEqual(self.op["operationId"], "GiteaDeleteComment")

    def test_route_still_requires_the_index_segment(self):
        """The URL shape is unchanged, so existing callers keep working."""
        names = {p["name"] for p in self.op["parameters"] if p["in"] == "path"}
        self.assertEqual(names, {"owner", "repo", "index", "comment_id"})

    def test_docstring_states_index_is_ignored(self):
        self.assertIn("ignored", self.op["description"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
