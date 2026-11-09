"""Regression tests for the six ported Gitea API fixes.

Each test fails if its fix is reverted, so these pin the post-port contract:

1. GiteaSubmitPRReview sends `event` (uppercase) AND legacy `type`, and
   rejects `changes_requested` with an empty body (Gitea refuses it).
2. GiteaGetPRDiff calls /pulls/{n}.diff -- the only route Gitea serves.
3. GiteaListPRComments walks reviews, falling back to the flat endpoint.
4. GiteaGetPRPipeline attributes a run by head_sha, NOT by branch: PR runs
   carry head_branch: null on Gitea, so branch filtering found nothing and
   answered "no runs" on green PRs. A missing run reports status "unknown",
   never "pending".
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
    def test_runs_are_queried_by_event_without_a_branch_filter(self):
        """PR runs carry head_branch: null, so a branch filter matches nothing.

        Live Gitea 28.1.0 returns zero runs for a PR when branch= is sent, on
        PRs that do have green runs. Event + limit only is what finds them.
        """
        calls = []

        async def recorder(method, url, **kwargs):
            calls.append((method, str(url), kwargs))
            if str(url).endswith("/actions/runs"):
                return {"workflow_runs": [{"id": 9, "status": "success",
                                           "head_sha": "abc123"}]}
            if "/pulls/3" in str(url):
                return {"head": {"ref": "feat/x", "sha": "abc123"}}
            return {}

        original = gitea_mod._gitea_request
        gitea_mod._gitea_request = recorder
        try:
            r = self.client.get("/gitea/pulls/LLM/demo/3/pipeline")
        finally:
            gitea_mod._gitea_request = original

        self.assertEqual(r.status_code, 200, r.text)
        params = [c for c in calls if c[1].endswith("/actions/runs")][0][2]["params"]
        self.assertEqual(params.get("event"), "pull_request")
        self.assertNotIn("branch", params)
        self.assertNotIn("ref", params)

    def test_run_is_attributed_by_head_sha(self):
        """The join key is head_sha, so an unrelated run must not be selected."""
        self.fake.route(
            "GET", "/pulls/3", FakeResponse(200, {"head": {"ref": "feat/x", "sha": "mine"}})
        )
        self.fake.route(
            "GET",
            "/actions/runs",
            FakeResponse(200, {"workflow_runs": [
                {"id": 1, "head_sha": "someone-else", "status": "failure"},
                {"id": 2, "head_sha": "mine", "status": "success", "conclusion": "success"},
            ]}),
        )
        body = self.client.get("/gitea/pulls/LLM/demo/3/pipeline").json()
        self.assertEqual(body["run_id"], 2)
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["matched_by"], "head_sha")

    def test_unattributable_run_is_unknown_not_pending(self):
        """'pending' invites polling; the honest answer is that no run matched."""
        self.fake.route(
            "GET", "/pulls/3", FakeResponse(200, {"head": {"ref": "feat/x", "sha": "mine"}})
        )
        self.fake.route(
            "GET", "/actions/runs", FakeResponse(200, {"workflow_runs": [
                {"id": 5, "head_sha": "unrelated", "status": "success"},
            ]}),
        )
        body = self.client.get("/gitea/pulls/LLM/demo/3/pipeline").json()
        self.assertEqual(body["status"], "unknown")
        self.assertEqual(body["run_id"], 0)
        self.assertEqual(body["matched_by"], "")
        self.assertIn("No workflow run found", body["display_title"])

    def test_merge_commit_is_the_fallback_when_head_has_no_run(self):
        """A merged PR's run is on the merge commit, not the head SHA."""
        self.fake.route(
            "GET",
            "/pulls/3",
            FakeResponse(200, {"head": {"ref": "refs/pull/3/head", "sha": "head"},
                               "merge_commit_sha": "merge"}),
        )
        self.fake.route(
            "GET", "/actions/runs", FakeResponse(200, {"workflow_runs": [
                {"id": 7, "head_sha": "merge", "status": "success", "conclusion": "success"},
            ]}),
        )
        body = self.client.get("/gitea/pulls/LLM/demo/3/pipeline").json()
        self.assertEqual(body["run_id"], 7)
        self.assertEqual(body["matched_by"], "merge_commit_sha")

    def test_closed_pr_branch_name_comes_from_label(self):
        """Gitea rewrites head.ref to refs/pull/N/head once a PR closes."""
        self.fake.route(
            "GET",
            "/pulls/3",
            FakeResponse(200, {"state": "closed",
                               "head": {"ref": "refs/pull/3/head", "label": "feat/x",
                                        "sha": "head"}}),
        )
        self.fake.route(
            "GET", "/actions/runs", FakeResponse(200, {"workflow_runs": [
                {"id": 7, "head_sha": "head", "status": "success"},
            ]}),
        )
        body = self.client.get("/gitea/pulls/LLM/demo/3/pipeline").json()
        self.assertEqual(body["branch"], "feat/x")

    def test_forgejo_field_names_are_mapped(self):
        self.fake.route(
            "GET", "/pulls/3", FakeResponse(200, {"head": {"ref": "feat/x", "sha": "abc123"}})
        )
        self.fake.route(
            "GET",
            "/actions/runs",
            FakeResponse(200, {"workflow_runs": [
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
            ]}),
        )
        body = self.client.get("/gitea/pulls/LLM/demo/3/pipeline").json()
        self.assertEqual(body["run_id"], 9)
        self.assertEqual(body["name"], "CI")
        self.assertEqual(body["branch"], "feat/x")
        self.assertEqual(body["commit_sha"], "abc123")
        self.assertEqual(body["started_at"], "2026-01-01T00:00:00Z")


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
