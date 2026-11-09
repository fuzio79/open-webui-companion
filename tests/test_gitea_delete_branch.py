"""Tests for GiteaDeleteBranch.

The only irreversible tool in the set, so every test asserts two things: the
outcome AND how many DELETE calls reached upstream. A guard that still mutated
would be worse than no guard.

Two guards exist because Gitea's own behaviour is unsafe. Both were established
against live 28.1.0:

  - deleting the default branch -> Gitea 403 (the guard is for a clear reason)
  - deleting an OPEN PR's head  -> Gitea **204**, PR left open against a branch
    that no longer exists, then unmergeable and unclosable. Silent-wrong-success.
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

REPO_INFO = {"name": "demo", "default_branch": "main", "full_name": "LLM/demo"}


def branch_payload(name="feat/x", sha="a" * 40):
    return {"name": name, "commit": {"id": sha}}


def pr(number, head, state="open"):
    return {"number": number, "state": state, "head": {"ref": head, "label": head}}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def delete(self, branch, **params):
        return self.client.request(
            "DELETE", f"/gitea/branches/LLM/demo/{branch}", params=params
        )

    def deletes(self):
        return self.fake.calls_matching("DELETE", "/branches/")

    def route(self, method, contains, response):
        self.fake.route(method, contains, response)

    def repo_info(self, payload=None):
        # APPEND, never prepend. FakeGitea returns the FIRST substring match, so a
        # '/repos/LLM/demo' route placed ahead of '/repos/LLM/demo/branches/...'
        # answers the branch GET with a repo payload (no 'commit' -> sha '').
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, payload or REPO_INFO))

    def ok_path(self, branch="feat/x", prs=None, sha="a" * 40):
        # Order matters: FakeGitea matches on substrings and returns the FIRST
        # hit, so a '/branches/' route registered before '/branches/feat/x'
        # would answer the GET with a DELETE-shaped response.
        self.fake.route("GET", "/pulls", FakeResponse(200, prs if prs is not None else []))
        self.fake.route(
            "GET", "/branches/" + branch, FakeResponse(200, branch_payload(branch, sha))
        )
        self.fake.route("DELETE", "/branches/" + branch, FakeResponse(204, None, text=""))
        self.repo_info()


class TestHappyPath(Base):
    def test_deletes_branch_and_reports_the_sha(self):
        self.ok_path()
        response = self.delete("feat/x")
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body["deleted"])
        self.assertFalse(body["already_absent"])
        self.assertEqual("a" * 40, body["sha"])
        self.assertEqual("feat/x", body["branch"])
        self.assertEqual([], body["warnings"])

    def test_branch_with_a_slash_is_passed_through_unencoded(self):
        """Gitea answers 400 to /branches/feat%2Fx (path decoded before routing)."""
        self.ok_path()
        response = self.delete("feat/nested/thing")
        self.assertEqual(200, response.status_code, response.text)
        url = self.deletes()[0][1]
        self.assertIn("/branches/feat/nested/thing", url)
        self.assertNotIn("%2F", url)

    def test_already_absent_is_success_not_an_error(self):
        """A retried delete must be safe: intent holds, nothing destroyed."""
        self.fake.route("GET", "/pulls", FakeResponse(200, []))
        self.fake.route("GET", "/branches/gone", FakeResponse(404, None, text="{}"))
        self.repo_info()
        response = self.delete("gone")
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body["deleted"])
        self.assertTrue(body["already_absent"])
        self.assertEqual([], self.deletes(), "nothing should be deleted")


class TestDefaultBranchGuard(Base):
    def test_default_branch_is_refused_before_any_delete(self):
        self.repo_info()
        response = self.delete("main")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("default_branch", response.json()["detail"]["blocked_reason"])
        self.assertEqual([], self.deletes(), "a refusal must not delete")

    def test_ordering_default_branch_wins_over_open_pr(self):
        """`main` exists and may have PRs -- the default-branch reason is the useful one."""
        self.fake.route("GET", "/pulls", FakeResponse(200, [pr(1, "main")]))
        self.fake.route("GET", "/branches/main", FakeResponse(200, branch_payload("main")))
        self.repo_info()
        response = self.delete("main")
        self.assertEqual("default_branch", response.json()["detail"]["blocked_reason"])
        self.assertEqual([], self.deletes())

    def test_confirm_flag_reaches_gitea_and_403_is_reported_clearly(self):
        self.fake.route("GET", "/pulls", FakeResponse(200, []))
        self.fake.route("GET", "/branches/main", FakeResponse(200, branch_payload("main")))
        self.fake.route(
            "DELETE",
            "/branches/",
            FakeResponse(
                403, None,
                text='{"message":"can not delete default or pull request target branch"}',
            ),
        )
        self.repo_info()
        response = self.delete("main", confirm_default_branch="true")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(1, len(self.deletes()))


class TestOpenPullRequestGuard(Base):
    """Gitea deletes an open PR's head branch (204) and wedges the PR."""

    def test_open_pr_head_is_refused_before_any_delete(self):
        self.fake.route("GET", "/pulls", FakeResponse(200, [pr(7, "feat/x")]))
        self.fake.route("GET", "/branches/feat/x", FakeResponse(200, branch_payload()))
        self.repo_info()
        response = self.delete("feat/x")
        self.assertEqual(409, response.status_code, response.text)
        detail = response.json()["detail"]
        self.assertEqual("open_pull_request", detail["blocked_reason"])
        self.assertEqual([7], detail["pull_requests"])
        self.assertIn("GiteaMergePR", detail["recovery"])
        self.assertEqual([], self.deletes(), "a refusal must not delete")

    def test_pr_from_a_different_branch_does_not_block(self):
        self.fake.route("GET", "/pulls", FakeResponse(200, [pr(9, "other/branch")]))
        self.fake.route("GET", "/branches/feat/x", FakeResponse(200, branch_payload()))
        self.fake.route("DELETE", "/branches/feat/x", FakeResponse(204, None, text=""))
        self.repo_info()
        response = self.delete("feat/x")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(self.deletes()))

    def test_closed_pr_does_not_block(self):
        self.fake.route("GET", "/pulls", FakeResponse(200, []))
        self.fake.route("GET", "/branches/feat/x", FakeResponse(200, branch_payload()))
        self.fake.route("DELETE", "/branches/feat/x", FakeResponse(204, None, text=""))
        self.repo_info()
        self.assertEqual(200, self.delete("feat/x").status_code)

    def test_unreadable_pr_list_warns_instead_of_blocking(self):
        """A failed listing must not make the tool unusable -- but say so."""
        self.fake.route("GET", "/pulls", FakeResponse(500, {"message": "boom"}))
        self.fake.route("GET", "/branches/feat/x", FakeResponse(200, branch_payload()))
        self.fake.route("DELETE", "/branches/feat/x", FakeResponse(204, None, text=""))
        self.repo_info()
        response = self.delete("feat/x")
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()["warnings"])
        self.assertIn("guard was not", response.json()["warnings"][0])


class TestWriteFailures(Base):
    def test_protected_branch_403_is_explained(self):
        self.fake.route("GET", "/pulls", FakeResponse(200, []))
        self.fake.route("GET", "/branches/feat/x", FakeResponse(200, branch_payload()))
        self.fake.route(
            "DELETE", "/branches/",
            FakeResponse(403, None, text='{"message":"branch is protected"}'),
        )
        self.repo_info()
        response = self.delete("feat/x")
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("protected", response.json()["detail"])

    def test_no_branch_name_is_rejected(self):
        response = self.client.request("DELETE", "/gitea/branches/LLM/demo/")
        self.assertIn(response.status_code, (404, 405, 422))


class TestOpenWebUICompatibility(Base):
    def test_flat_path_params_are_tolerated(self):
        self.ok_path()
        response = self.client.request(
            "DELETE",
            "/gitea/branches/LLM/demo/feat/x",
            params={"owner": "LLM", "repo": "demo", "branch": "feat/x"},
        )
        self.assertEqual(200, response.status_code, response.text)

    def test_operation_id_and_unknown_field_handling(self):
        spec = self.client.get("/openapi.json").json()
        op = spec["paths"]["/gitea/branches/{owner}/{repo}/{branch}"]["delete"]
        self.assertEqual("GiteaDeleteBranch", op["operationId"])


if __name__ == "__main__":
    unittest.main()
