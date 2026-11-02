"""Tests for GiteaListBranches.

The route is read-only and takes no request body, so two failure classes matter:

1. Returning Gitea's raw payload instead of a bounded shape would leak an
   unbounded blob into the model's context.
2. A `default_branch` lookup failure must not turn a successful listing into an
   error -- an empty list reads as "this repository has no branches", which is a
   wrong answer rather than a failed call.

The fake routes on URL substrings, so it cannot tell `/branches` from
`/branches/x`; these tests therefore assert on the URL and params we send.
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

from app import gitea as gitea_mod
from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

BRANCHES = [
    {
        "name": "main",
        "protected": True,
        "effective_branch_protection_name": "main",
        "commit": {"id": "a" * 40, "message": "tip"},
    },
    {
        "name": "feature/example-work",
        "protected": False,
        "commit": {"id": "b" * 40, "message": "other"},
    },
]
REPO = {"name": "demo", "default_branch": "main", "full_name": "LLM/demo"}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def get(self, path="/gitea/branches/LLM/demo", **kw):
        return self.client.get(path, **kw)

    def branch_call(self):
        calls = self.fake.calls_matching("GET", "/branches")
        self.assertTrue(calls, "no branch call was made")
        return calls[0]


class TestShape(Base):
    def test_returns_bounded_branch_info(self):
        self.fake.route("GET", "/branches", FakeResponse(200, BRANCHES))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO))
        r = self.get()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            body["branches"],
            [
                {"name": "main", "sha": "a" * 40, "protected": True},
                {"name": "feature/example-work", "sha": "b" * 40, "protected": False},
            ],
        )

    def test_raw_payload_is_not_passed_through(self):
        """The commit message and other raw fields must not leak."""
        self.fake.route("GET", "/branches", FakeResponse(200, BRANCHES))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO))
        body = self.get().json()
        self.assertEqual(set(body["branches"][0]), {"name", "sha", "protected"})

    def test_includes_default_branch(self):
        self.fake.route("GET", "/branches", FakeResponse(200, BRANCHES))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO))
        self.assertEqual(self.get().json()["default_branch"], "main")

    def test_default_branch_as_object_is_tolerated(self):
        """Forgejo has reported this as an object; do not emit a repr."""
        self.fake.route("GET", "/branches", FakeResponse(200, BRANCHES))
        self.fake.route(
            "GET",
            "/repos/LLM/demo",
            FakeResponse(200, {"default_branch": {"name": "main"}}),
        )
        self.assertEqual(self.get().json()["default_branch"], "main")

    def test_protection_inferred_from_effective_rule(self):
        self.fake.route(
            "GET",
            "/branches",
            FakeResponse(200, [{"name": "x", "effective_branch_protection_name": "r"}]),
        )
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO))
        self.assertTrue(self.get().json()["branches"][0]["protected"])

    def test_branch_without_commit_does_not_raise(self):
        self.fake.route("GET", "/branches", FakeResponse(200, [{"name": "bare"}]))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO))
        r = self.get()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["branches"][0]["sha"], "")


class TestQueryParams(Base):
    """Assert the params we hand to httpx.

    FakeGitea records only (method, url, json) and drops kwargs, so `params`
    cannot be observed through it. This uses the recorder pattern from
    test_port_api_fixes.py, which patches _gitea_request directly.
    """

    def captured_params(self, **query):
        calls = []

        async def recorder(method, url, **kwargs):
            calls.append((str(url), kwargs))
            if str(url).endswith("/branches"):
                return list(BRANCHES)
            return dict(REPO)

        original = gitea_mod._gitea_request
        gitea_mod._gitea_request = recorder
        try:
            r = self.client.get("/gitea/branches/LLM/demo", params=query)
        finally:
            gitea_mod._gitea_request = original
        self.assertEqual(r.status_code, 200, r.text)
        branch_calls = [c for c in calls if c[0].endswith("/branches")]
        self.assertEqual(len(branch_calls), 1)
        return branch_calls[0][1].get("params")

    def test_pagination_is_forwarded(self):
        self.assertEqual(
            self.captured_params(page=2, limit=10), {"page": 2, "limit": 10}
        )

    def test_filter_is_forwarded_only_when_set(self):
        self.assertEqual(self.captured_params(q="plan"), {"page": 1, "limit": 50, "q": "plan"})

    def test_empty_filter_is_omitted(self):
        """`q=` would ask Gitea to filter on an empty string."""
        self.assertEqual(self.captured_params(), {"page": 1, "limit": 50})

    def test_defaults_are_pages_of_fifty(self):
        self.assertEqual(self.captured_params(), {"page": 1, "limit": 50})

    def test_limit_above_maximum_is_rejected_locally(self):
        r = self.get(params={"limit": 101})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.fake.calls_matching("GET", "/branches"), [])


class TestFailureModes(Base):
    def test_upstream_404_passes_through(self):
        self.fake.route("GET", "/branches", FakeResponse(404, {"message": "no such repo"}))
        r = self.get()
        self.assertEqual(r.status_code, 404, r.text)

    def test_non_list_response_is_an_error_not_an_empty_list(self):
        """Returning total=0 here would silently claim the repo has no branches."""
        self.fake.route("GET", "/branches", FakeResponse(200, {"message": "unexpected"}))
        r = self.get()
        self.assertEqual(r.status_code, 502, r.text)
        self.assertNotIn("branches", r.json())

    def test_default_branch_failure_still_returns_the_branches(self):
        """The repo read failing must not discard a good listing."""
        self.fake.route("GET", "/branches", FakeResponse(200, BRANCHES))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(500, {"message": "boom"}))
        r = self.get()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 2)
        self.assertEqual(r.json()["default_branch"], "")

    def test_empty_repository_returns_zero_not_error(self):
        self.fake.route("GET", "/branches", FakeResponse(200, []))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO))
        r = self.get()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"total": 0, "branches": [], "default_branch": "main"})


class TestRouting(unittest.TestCase):
    def test_operation_id_and_no_body(self):
        spec = TestClient(app).get("/openapi.json").json()
        op = spec["paths"]["/gitea/branches/{owner}/{repo}"]["get"]
        self.assertEqual(op["operationId"], "GiteaListBranches")
        self.assertNotIn("requestBody", op, "a GET with no body must not advertise one")

    def test_path_matches_pattern_not_a_different_route(self):
        """Guard the URL we actually call, not just the response."""
        fake = FakeGitea()
        orig = httpx.AsyncClient
        fake.install(httpx)
        try:
            fake.route("GET", "/branches", FakeResponse(200, []))
            fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO))
            TestClient(app).get("/gitea/branches/LLM/demo")
            urls = [c[1] for c in fake.calls_matching("GET", "/branches")]
            self.assertEqual(len(urls), 1)
            self.assertTrue(
                urls[0].endswith("/repos/LLM/demo/branches"),
                f"unexpected upstream URL: {urls[0]}",
            )
        finally:
            httpx.AsyncClient = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBranchCreateModel(unittest.TestCase):
    """Step 1: the body model must be callable from Open-WebUI.

    `GiteaCreateBranch` itself is step 3; only the contract is pinned here.
    """

    def test_model_tolerates_echoed_path_params(self):
        from app.gitea import BranchCreate

        m = BranchCreate(
            new_branch_name="feat/x", owner="LLM", repo="demo"
        )
        self.assertEqual(m.new_branch_name, "feat/x")
        # Echoed route params must not be laundered into the outgoing payload.
        self.assertEqual(
            m.model_dump(exclude_none=True), {"new_branch_name": "feat/x", "old_ref_name": ""}
        )

    def test_typo_is_rejected(self):
        """extra=\"forbid\" must survive: a typo cannot be a silent no-op."""
        from pydantic import ValidationError

        from app.gitea import BranchCreate

        with self.assertRaises(ValidationError):
            BranchCreate(new_branch="feat/x")

    def test_old_ref_name_is_optional(self):
        from app.gitea import BranchCreate

        self.assertEqual(BranchCreate(new_branch_name="x").old_ref_name, "")
