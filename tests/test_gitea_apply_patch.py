"""Tests for GiteaApplyFilePatch."""

import json
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
from app.errors import GiteaError
from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

COMMIT_SHA = "c" * 40
BODY = dict(
    content="diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n",
    message="edit",
)


def commit_payload(sha=COMMIT_SHA):
    return {
        "content": None,
        "commit": {
            "sha": sha,
            "html_url": "https://gitea.example/LLM/demo/commit/" + sha,
            "message": "edit",
        },
    }


def apply_error(text, status=500):
    return FakeResponse(
        status, None, text=json.dumps({"message": "git apply error: exit status 1 - error: " + text})
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, {"default_branch": "main"}))
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def post(self, body=None):
        payload = dict(BODY)
        payload.update(body or {})
        return self.client.post("/gitea/patches/LLM/demo", json=payload)

    def route_first(self, method, contains, response):
        """Register a specific route.

        Kept as a named helper for readability only: the fake now picks the most
        specific matching route, so registration order does not matter.
        """
        self.fake.route_first(method, contains, response)

    def posts(self):
        return self.fake.calls_matching("POST", "diffpatch")

    def reason(self, response):
        return response.json()["detail"]["blocked_reason"]


class TestHappyPath(Base):
    def test_applies_patch_and_reports_gitea_commit(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.post()
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(COMMIT_SHA, data["commit_sha"])
        self.assertEqual("main", data["branch"])
        self.assertEqual(["f.txt"], data["files_changed"])
        self.assertEqual(1, data["lines_added"])
        self.assertEqual(1, data["lines_removed"])

    def test_posts_the_documented_payload(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        self.post()
        call = self.posts()[0]
        self.assertTrue(call[1].endswith("/api/v1/repos/LLM/demo/diffpatch"), call[1])
        self.assertEqual("edit", call[2]["message"])
        self.assertEqual("main", call[2]["branch"])
        self.assertIn("@@ -1,3 +1,3 @@", call[2]["content"])
        self.assertNotIn("new_branch", call[2])

    def test_default_branch_is_resolved_not_guessed(self):
        # Replace, not prepend: the key collides with setUp's route, and a dict
        # merge would keep the OLD value under the same key.
        self.fake.routes = {}
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, {"default_branch": "trunk"}))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.post()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("trunk", self.posts()[0][2]["branch"])

    def test_explicit_branch_is_used(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        self.post({"branch": "feature/x"})
        self.assertEqual("feature/x", self.posts()[0][2]["branch"])

    def test_new_branch_is_created_and_reported(self):
        self.route_first("GET", "/branches/feature", FakeResponse(404, None, text="{}"))
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.post({"branch": "main", "new_branch": "feature"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("feature", self.posts()[0][2]["new_branch"])
        self.assertEqual("feature", response.json()["branch"])
        self.assertEqual("feature", response.json()["new_branch"])

    def test_missing_final_newline_is_normalised(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        self.post({"content": BODY["content"].rstrip("\n")})
        self.assertTrue(self.posts()[0][2]["content"].endswith("\n"))

    def test_unresolvable_default_branch_fails_loudly(self):
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, {}))
        response = self.post()
        self.assertEqual(502, response.status_code, response.text)
        self.assertEqual([], self.posts())


class TestPreflightRefusals(Base):
    def test_empty_diff(self):
        response = self.post({"content": "   \n"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("empty_diff", self.reason(response))
        self.assertEqual([], self.posts())

    def test_body_without_a_hunk_is_not_a_patch(self):
        response = self.post({"content": "hello world\n"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("not_a_patch", self.reason(response))
        self.assertEqual([], self.posts())

    def test_no_eol_marker_is_refused_and_names_the_alternative(self):
        marker = "\\ No newline at end of file\n"
        response = self.post({"content": BODY["content"] + marker})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("no_eol_marker", self.reason(response))
        self.assertIn("GiteaEditFile", json.dumps(response.json()))
        self.assertEqual([], self.posts())

    def test_refusals_carry_a_recovery_hint(self):
        response = self.post({"content": ""})
        self.assertTrue(response.json()["detail"]["recovery"])


class TestBranchGuards(Base):
    def test_new_branch_equal_to_base_branch_is_refused(self):
        response = self.post({"branch": "main", "new_branch": "main"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("base_branch_echo", self.reason(response))
        self.assertEqual([], self.posts())

    def test_new_branch_equal_to_resolved_default_is_refused(self):
        response = self.post({"new_branch": "main"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("base_branch_echo", self.reason(response))
        self.assertEqual([], self.posts())

    def test_ordering_base_branch_is_reported_as_echo_not_exists(self):
        self.route_first("GET", "/branches/main", FakeResponse(200, {"name": "main"}))
        response = self.post({"branch": "main", "new_branch": "main"})
        self.assertEqual("base_branch_echo", self.reason(response))
        self.assertEqual([], self.posts())

    def test_existing_new_branch_is_refused_without_writing(self):
        self.route_first("GET", "/branches/other", FakeResponse(200, {"name": "other"}))
        response = self.post({"branch": "main", "new_branch": "other"})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("new_branch_exists", self.reason(response))
        self.assertEqual([], self.posts())

    def test_allow_base_branch_commit_permits_it(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.post(
            {"branch": "main", "new_branch": "main", "allow_base_branch_commit": True}
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("main", self.posts()[0][2]["new_branch"])


class TestBranchNamesWithSlashes(Base):
    """A ``feat/...`` new_branch must not be percent-encoded.

    Live Gitea answers 400 to ``/branches/feat%2Ftwo``: the path is decoded
    before routing, so the slash reads as a separator. Encoding it aborted every
    tool that used a slashed new branch -- the usual naming convention -- with a
    bare ``400`` and no ``blocked_reason``.
    """

    def test_slashed_new_branch_is_checked_unencoded(self):
        seen = []

        async def recorder(method, url, **kwargs):
            seen.append(str(url))
            if str(url).endswith("/diffpatch"):
                return commit_payload()
            raise GiteaError("not found", status_code=404)

        original = gitea_mod._gitea_request
        gitea_mod._gitea_request = recorder
        try:
            response = self.post({"branch": "main", "new_branch": "feat/two"})
        finally:
            gitea_mod._gitea_request = original

        self.assertEqual(200, response.status_code, response.text)
        branch_calls = [u for u in seen if "/branches/" in u]
        self.assertEqual(1, len(branch_calls), seen)
        self.assertIn("/branches/feat/two", branch_calls[0])
        self.assertNotIn("%2F", branch_calls[0])

    def test_existing_slashed_branch_is_refused(self):
        self.route_first("GET", "/branches/feat/one",
                         FakeResponse(200, {"name": "feat/one"}))
        response = self.post({"branch": "main", "new_branch": "feat/one"})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("new_branch_exists", self.reason(response))
        self.assertEqual([], self.posts())


class TestUpstreamErrorMapping(Base):
    def check(self, upstream_text, expected):
        self.fake.route("POST", "/diffpatch", apply_error(upstream_text))
        response = self.post()
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual(expected, self.reason(response))
        return response

    def test_no_valid_patches(self):
        self.check("No valid patches in input (allow with --allow-empty)", "not_a_patch")

    def test_file_not_found(self):
        self.check("ghost.txt: does not exist in index", "file_not_found")

    def test_path_rejected(self):
        self.check("invalid path '../evil.txt'", "path_rejected")

    def test_corrupt_patch(self):
        self.check("corrupt patch at <stdin>:8", "malformed_patch")

    def test_context_mismatch(self):
        self.check("patch failed: f.txt:1\nerror: f.txt: patch does not apply",
                   "context_mismatch")

    def test_raw_git_apply_text_is_not_the_whole_answer(self):
        response = self.check("patch failed: f.txt:1", "context_mismatch")
        self.assertTrue(response.json()["detail"]["recovery"])


class TestSuccessIsNotAssumed(Base):
    def test_201_without_a_commit_sha_is_not_reported_as_success(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, {"content": None}))
        response = self.post()
        self.assertEqual(502, response.status_code, response.text)
        self.assertNotIn("commit_sha", response.json())


class TestOpenWebUICompatibility(Base):
    def test_flat_path_params_in_body_are_tolerated(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, commit_payload()))
        response = self.post({"owner": "LLM", "repo": "demo"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn("owner", self.posts()[0][2])

    def test_unknown_field_is_still_rejected(self):
        response = self.post({"contnet": "typo"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual([], self.posts())

    def test_operation_id_is_published(self):
        spec = self.client.get("/openapi.json").json()
        op = spec["paths"]["/gitea/patches/{owner}/{repo}"]["post"]
        self.assertEqual("GiteaApplyFilePatch", op["operationId"])


if __name__ == "__main__":
    unittest.main()


class TestPathTraversal(Base):
    """A path outside the repo is its own reason.

    Live, Gitea reports `../evil` as 'does not exist in index' about as often
    as 'invalid path', so relying on the error text labelled a traversal as a
    missing file. The preflight check makes the reason accurate.
    """

    def test_parent_traversal_is_path_rejected(self):
        response = self.post({"content": "diff --git a/../evil b/../evil\n--- a/../evil\n+++ b/../evil\n@@ -1 +1 @@\n-a\n+b\n"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("path_rejected", self.reason(response))
        self.assertEqual([], self.posts())

    def test_absolute_path_is_path_rejected(self):
        response = self.post({"content": "diff --git a//etc/evil b//etc/evil\n--- /dev/null\n+++ b//etc/evil\n@@ -0,0 +1 @@\n+x\n"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertEqual("path_rejected", self.reason(response))
        self.assertEqual([], self.posts())

    def test_normal_paths_are_not_flagged(self):
        from app.gitea import _diff_paths_violating_repo
        self.assertEqual([], _diff_paths_violating_repo(BODY["content"]))
        self.assertEqual([], _diff_paths_violating_repo(
            "diff --git a/deep/dir/file.txt b/deep/dir/file.txt\n--- a/deep/dir/file.txt\n+++ b/deep/dir/file.txt\n@@ -1 +1 @@\n-a\n+b\n"))
