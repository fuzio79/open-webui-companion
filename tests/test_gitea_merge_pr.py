"""Tests for GiteaMergePR's refusal matrix.

Gitea's own refusals are misleading, so the tool decides before writing:

  closed-unmerged PR -> 404 "not found"   (reads as: wrong PR number)
  WIP PR             -> 405               (indistinguishable from other 405s)
  already merged     -> 405 "already merged"

and `mergeable` compounds it: a merged PR reports mergeable:true and a WIP PR
reports mergeable:false. So the check order MUST be

    merged -> state -> draft -> mergeable

The TestCheckOrdering class fails if that order is ever "simplified".

FakeGitea matches URL substrings and knows nothing about Gitea's semantics, so
these tests pin WHAT WE SEND and WHICH refusal we choose. The upstream shapes
were verified live (recorded in the PR body).
"""

import os
import pathlib
import sys
import unittest

os.environ.setdefault("GITEA_TOKEN", "test-token")
os.environ.setdefault("GITEA_INSTANCE_URL", "https://gitea.example")
os.environ.setdefault("EVIDENCE_DIR", "/tmp/owc-test-data")
# No wall-clock waiting in tests: the mergeable re-check would otherwise add
# seconds per conflict test.
os.environ["MERGEABLE_RECHECK_DELAY"] = "0"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

REPO_INFO = {"default_merge_style": "squash", "full_name": "LLM/demo"}


def pr(state="open", merged=False, mergeable=True, draft=False, sha=""):
    return {
        "number": 5,
        "state": state,
        "merged": merged,
        "mergeable": mergeable,
        "draft": draft,
        "merge_commit_sha": sha,
        "merged_at": "2026-11-02T10:00:00+01:00" if merged else None,
        "base": {"ref": "main"},
        "head": {"ref": "feat"},
        "html_url": "https://gitea.example/LLM/demo/pulls/5",
    }


async def _no_sleep(*args, **kwargs):
    """Stand-in for asyncio.sleep so tests never wait on wall-clock."""
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

        # The mergeable re-check sleeps between re-reads. Tests must not wait on
        # wall-clock, and the constant is bound at import time (another test
        # module imports app first), so patch sleep rather than the env var.
        import app.gitea as _g

        self._orig_sleep = _g.asyncio.sleep
        _g.asyncio.sleep = _no_sleep

    def tearDown(self):
        import app.gitea as _g

        _g.asyncio.sleep = self._orig_sleep
        httpx.AsyncClient = self._orig

    def merge(self, body=None):
        return self.client.post("/gitea/pulls/LLM/demo/5/merge", json=body or {})

    def merges(self):
        return self.fake.calls_matching("POST", "/merge")

    def reason(self, response):
        return response.json()["detail"]["blocked_reason"]

    def stub_pr(self, **kw):
        self.fake.route("GET", "/pulls/5", FakeResponse(200, pr(**kw)))

    def stub_repo(self):
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO_INFO))


class TestSuccess(Base):
    def test_merges_and_reports_stored_state(self):
        """The response must come from a re-read, not from assumed success.

        The GET is stateful: unmerged before the POST, merged after. A response
        built from the request instead of the stored state would still report
        merged=True here, so the assertion that matters is merge_commit_sha --
        which only exists after the re-read.
        """
        state = {"merged": False}

        def pr_reader(url, body):
            if state["merged"]:
                return FakeResponse(200, pr(state="closed", merged=True, sha="c" * 40))
            return FakeResponse(200, pr())

        def merge_writer(url, body):
            state["merged"] = True
            return FakeResponse(200, "")

        self.fake.route("GET", "/pulls/5", pr_reader)
        # Register the specific route BEFORE the repo route. FakeGitea matches
        # URL substrings in insertion order, and "/repos/LLM/demo" is a prefix of
        # "/repos/LLM/demo/pulls/5", so the repo stub would shadow the PR stub.
        self.fake.route("POST", "/merge", merge_writer)
        self.stub_repo()

        r = self.merge()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["merged"])
        self.assertEqual(body["merge_commit_sha"], "c" * 40, "re-read, not assumed")
        self.assertEqual(body["state"] if "state" in body else "closed", "closed")
        self.assertEqual(body["do"], "squash")

    def test_style_defaults_to_repo_policy_not_merge(self):
        """Hardcoding 'merge' would overrule per-repo policy."""
        self.stub_pr()
        self.stub_repo()
        self.fake.route("POST", "/merge", FakeResponse(200, ""))
        self.merge()
        self.assertEqual(self.merges()[0][2], {"do": "squash"})

    def test_explicit_style_wins(self):
        self.stub_pr()
        self.stub_repo()
        self.fake.route("POST", "/merge", FakeResponse(200, ""))
        self.merge({"do": "rebase"})
        self.assertEqual(self.merges()[0][2], {"do": "rebase"})

    def test_empty_body_still_reaches_gitea(self):
        self.stub_pr()
        self.stub_repo()
        self.fake.route("POST", "/merge", FakeResponse(200, ""))
        r = self.client.post("/gitea/pulls/LLM/demo/5/merge", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.merges()), 1)

    def test_force_merge_and_delete_branch_default_off(self):
        self.stub_pr()
        self.stub_repo()
        self.fake.route("POST", "/merge", FakeResponse(200, ""))
        self.merge()
        sent = self.merges()[0][2]
        self.assertNotIn("force_merge", sent)
        self.assertNotIn("delete_branch_after_merge", sent)


class TestRefusals(Base):
    def test_already_merged_is_idempotent_success_not_a_refusal(self):
        """Re-merging what is already merged is not a caller error.

        Returning 409 here would make a retrying agent believe the merge failed,
        which is the opposite of the truth. Nothing is written, and Gitea's
        original merge_commit_sha is preserved rather than a new merge claimed.
        """
        self.stub_pr(state="closed", merged=True, sha="d" * 40)
        r = self.merge()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["merged"])
        self.assertTrue(body["already_merged"])
        self.assertEqual(body["merge_commit_sha"], "d" * 40, "original sha preserved")
        self.assertEqual(self.merges(), [], "must not attempt a merge")

    def test_closed_unmerged_is_not_a_bare_404(self):
        """Upstream answers 404 here, which reads as 'wrong PR number'."""
        self.stub_pr(state="closed", merged=False, mergeable=False)
        r = self.merge()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.reason(r), "closed_unmerged")
        self.assertEqual(self.merges(), [])

    def test_draft(self):
        self.stub_pr(draft=True, mergeable=False)
        r = self.merge()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.reason(r), "is_draft")
        self.assertEqual(self.merges(), [])

    def test_conflicts(self):
        self.stub_pr(mergeable=False)
        r = self.merge()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.reason(r), "conflicts")
        self.assertEqual(self.merges(), [])

    def test_not_found(self):
        self.fake.route("GET", "/pulls/5", FakeResponse(404, {"message": "not found"}))
        r = self.merge()
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.reason(r), "not_found")
        self.assertEqual(self.merges(), [])

    def test_refusal_carries_a_recovery_action(self):
        self.stub_pr(state="closed", merged=False, mergeable=False)
        detail = self.merge().json()["detail"]["detail"]
        self.assertIn("Reopen", detail)

    def test_draft_advice_points_at_the_fix_not_the_prefix(self):
        """Decision: advise clearing draft state, not hand-editing the title."""
        self.stub_pr(draft=True, mergeable=False)
        detail = self.merge().json()["detail"]["detail"]
        self.assertIn("draft=false", detail)
        self.assertNotIn("WIP:", detail)


class TestCheckOrdering(Base):
    """These fail if `mergeable` is tested before merged/state/draft."""

    def test_merged_pr_is_not_reported_mergeable(self):
        """merged:true AND mergeable:true -- the trap.

        Testing mergeable first would attempt a second merge. The correct result
        is idempotent success with no write.
        """
        self.stub_pr(state="closed", merged=True, mergeable=True, sha="e" * 40)
        r = self.merge()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["already_merged"])
        self.assertEqual(self.merges(), [], "must not attempt a second merge")

    def test_draft_is_not_reported_as_conflict(self):
        self.stub_pr(draft=True, mergeable=False)
        self.assertEqual(self.reason(self.merge()), "is_draft")

    def test_closed_is_not_reported_as_conflict(self):
        self.stub_pr(state="closed", merged=False, mergeable=False)
        self.assertEqual(self.reason(self.merge()), "closed_unmerged")


class TestStyleGuard(Base):
    def test_manually_merged_is_rejected_without_any_call(self):
        r = self.merge({"do": "manually-merged"})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.merges(), [], "must not reach Gitea")
        self.assertEqual(self.fake.calls_matching("GET", "/pulls/5"), [])

    def test_manually_merged_rejected_even_if_pr_would_be_blocked(self):
        self.stub_pr(state="closed", merged=False)
        r = self.merge({"do": "manually-merged"})
        self.assertEqual(r.status_code, 422, r.text)


class TestSpec(unittest.TestCase):
    def test_operation_id(self):
        spec = TestClient(app).get("/openapi.json").json()
        op = spec["paths"]["/gitea/pulls/{owner}/{repo}/{index}/merge"]["post"]
        self.assertEqual(op["operationId"], "GiteaMergePR")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMergeableIsComputedAsynchronously(Base):
    """Regression: a just-opened PR reports mergeable:false until Gitea checks.

    Found live: attempt 1 saw mergeable=false and was refused as a conflict;
    attempt 2 (2s later) saw true, and the merge succeeded. Treating the first
    false as a conflict is a wrong answer, so a false is re-read before refusing.
    """

    def test_transient_false_is_rechecked_then_merged(self):
        reads = {"n": 0}

        def pr_reader(url, body):
            reads["n"] += 1
            return FakeResponse(200, pr(mergeable=reads["n"] > 1))

        # Specific route first: "/repos/LLM/demo" is a prefix of the PR URL and
        # FakeGitea matches substrings in insertion order.
        self.fake.route("GET", "/pulls/5", pr_reader)
        self.fake.route("POST", "/merge", FakeResponse(200, ""))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO_INFO))

        r = self.merge()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(reads["n"], 1, "must have re-read mergeability")
        self.assertEqual(len(self.merges()), 1)

    def test_persistent_false_is_still_a_conflict(self):
        self.stub_pr(mergeable=False)
        r = self.merge()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.reason(r), "conflicts")
        self.assertEqual(self.merges(), [], "a real conflict must not merge")
