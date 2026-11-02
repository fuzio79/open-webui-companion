"""Regression tests for the six ported Gitea API fixes.

Each test fails if its fix is reverted, so these pin the post-port contract:

1. GiteaSubmitPRReview sends `event` (uppercase) AND legacy `type`, and
   rejects `changes_requested` with an empty body (Gitea refuses it).
2. GiteaGetPRDiff calls /pulls/{n}.diff -- the only route Gitea serves.
3. GiteaListPRComments walks reviews, falling back to the flat endpoint.
4. GiteaGetPRPipeline filters runs by branch AND ref (Gitea vs Forgejo).
5. Run field mapping tolerates Forgejo's key names.
6. GiteaGetPipelineOutput reads the {jobs,total_count} wrapper and never
   passes None into a str field (which would 500).
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

import app.gitea as gitea_mod
from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

DIFF = "diff --git a/x b/x\n+added\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig


class TestReviewPayload(Base):
    def test_approved_sends_event_and_legacy_type(self):
        self.fake.route("POST", "/pulls/3/reviews", FakeResponse(201, {"id": 1}))
        r = self.client.post(
            "/gitea/pulls/LLM/demo/3/reviews",
            json={"body": "lgtm", "type": "approved"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.fake.calls_matching("POST", "/reviews")[0][2]
        self.assertEqual(
            sent, {"event": "APPROVED", "type": "approve", "body": "lgtm"}
        )

    def test_changes_requested_without_body_is_rejected(self):
        """Gitea refuses REQUEST_CHANGES with no body; fail before the call."""
        r = self.client.post(
            "/gitea/pulls/LLM/demo/3/reviews", json={"type": "changes_requested"}
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.fake.calls_matching("POST", "/reviews"), [])

    def test_unknown_review_type_is_rejected(self):
        r = self.client.post(
            "/gitea/pulls/LLM/demo/3/reviews", json={"type": "bogus", "body": "x"}
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.fake.calls_matching("POST", "/reviews"), [])


class TestPRDiffRoute(Base):
    def test_uses_the_dot_diff_route(self):
        self.fake.route("GET", "/pulls/3.diff", FakeResponse(200, None, text=DIFF))
        r = self.client.get("/gitea/pulls/LLM/demo/3/diff")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("+added", r.text)
        url = self.fake.calls_matching("GET", "/pulls/3")[0][1]
        self.assertTrue(url.endswith("/pulls/3.diff"), url)
        self.assertNotIn("/pulls/3/diff", url)

    def test_style_param_is_no_longer_advertised(self):
        spec = TestClient(app).get("/openapi.json").json()
        op = spec["paths"]["/gitea/pulls/{owner}/{repo}/{index}/diff"]["get"]
        self.assertNotIn("style", [p["name"] for p in op.get("parameters", [])])


class TestPRCommentsWalk(Base):
    def test_walks_each_review_and_sorts_by_id(self):
        self.fake.route(
            "GET", "/pulls/5/reviews/11/comments", FakeResponse(200, [{"id": 2}])
        )
        self.fake.route(
            "GET", "/pulls/5/reviews/12/comments", FakeResponse(200, [{"id": 1}])
        )
        self.fake.route(
            "GET", "/pulls/5/reviews", FakeResponse(200, [{"id": 11}, {"id": 12}])
        )
        r = self.client.get("/gitea/pulls/LLM/demo/5/comments")
        self.assertEqual([c["id"] for c in r.json()], [1, 2])

    def test_falls_back_to_flat_endpoint_on_404(self):
        self.fake.route(
            "GET", "/pulls/5/reviews", FakeResponse(404, {"message": "nope"})
        )
        self.fake.route("GET", "/pulls/5/comments", FakeResponse(200, [{"id": 9}]))
        r = self.client.get("/gitea/pulls/LLM/demo/5/comments")
        self.assertEqual(r.json(), [{"id": 9}])

    def test_page_and_limit_are_no_longer_advertised(self):
        spec = TestClient(app).get("/openapi.json").json()
        op = spec["paths"]["/gitea/pulls/{owner}/{repo}/{index}/comments"]["get"]
        names = [p["name"] for p in op.get("parameters", [])]
        self.assertEqual(names, ["owner", "repo", "index"])


class TestPipelineQueries(Base):
    def test_runs_are_filtered_by_branch_and_ref(self):
        """Gitea filters by branch, Forgejo by ref -- both must be sent."""
        calls = []

        async def recorder(method, url, **kwargs):
            calls.append((method, str(url), kwargs))
            if str(url).endswith("/actions/runs"):
                return {"workflow_runs": [{"id": 9, "status": "success"}]}
            if "/pulls/3" in str(url):
                return {"head": {"ref": "feat/x"}}
            return {}

        original = gitea_mod._gitea_request
        gitea_mod._gitea_request = recorder
        try:
            r = self.client.get("/gitea/pulls/LLM/demo/3/pipeline")
        finally:
            gitea_mod._gitea_request = original

        self.assertEqual(r.status_code, 200, r.text)
        run_call = [c for c in calls if c[1].endswith("/actions/runs")][0]
        self.assertEqual(
            run_call[2].get("params"),
            {"branch": "feat/x", "ref": "feat/x", "event": "pull_request"},
        )

    def test_forgejo_field_names_are_mapped(self):
        self.fake.route(
            "GET", "/pulls/3", FakeResponse(200, {"head": {"ref": "feat/x"}})
        )
        self.fake.route(
            "GET",
            "/actions/runs",
            FakeResponse(
                200,
                {
                    "workflow_runs": [
                        {
                            "id": 9,
                            "title": "CI",
                            "prettyref": "feat/x",
                            "commit_sha": "abc123",
                            "started": "2026-01-01T00:00:00Z",
                            "updated": "2026-01-02T00:00:00Z",
                            "status": "success",
                            "conclusion": "success",
                            "event": "pull_request",
                        }
                    ]
                },
            ),
        )
        body = self.client.get("/gitea/pulls/LLM/demo/3/pipeline").json()
        self.assertEqual(body["run_id"], 9)
        self.assertEqual(body["name"], "CI")
        self.assertEqual(body["display_title"], "CI")
        self.assertEqual(body["branch"], "feat/x")
        self.assertEqual(body["commit_sha"], "abc123")
        self.assertEqual(body["started_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(body["updated_at"], "2026-01-02T00:00:00Z")


class TestPipelineOutput(Base):
    def test_jobs_wrapper_none_coercion_and_run_level_fallback(self):
        # Most specific first: FakeGitea matches routes in insertion order.
        self.fake.route(
            "GET", "/actions/runs/4/jobs/100/logs", FakeResponse(404, {"message": "no"})
        )
        self.fake.route(
            "GET", "/actions/jobs/100/logs", FakeResponse(404, {"message": "no"})
        )
        self.fake.route("GET", "/actions/runs/4/logs", FakeResponse(200, None, text="RUNLOG"))
        self.fake.route(
            "GET",
            "/actions/runs/4/jobs",
            FakeResponse(
                200,
                {
                    "jobs": [
                        {
                            "id": 100,
                            "name": "test",
                            "status": "success",
                            "conclusion": "success",
                            "started_at": None,
                            "url": None,
                        }
                    ],
                    "total_count": 1,
                },
            ),
        )
        self.fake.route(
            "GET",
            "/actions/runs/4",
            FakeResponse(200, {"id": 4, "name": "CI", "status": "success"}),
        )

        r = self.client.get("/gitea/actions/runs/LLM/demo/4/logs")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()

        # {jobs, total_count} wrapper must be read (was workflow_jobs only).
        self.assertEqual(len(body["jobs"]), 1)
        self.assertEqual(body["jobs"][0]["name"], "test")
        # None must be coerced to "" or the str fields 500.
        self.assertEqual(body["jobs"][0]["started_at"], "")
        self.assertEqual(body["jobs"][0]["url"], "")
        # Per-job logs 404 -> run-level logs must be surfaced.
        self.assertIn("RUNLOG", body["logs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
