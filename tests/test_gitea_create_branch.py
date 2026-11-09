"""Tests for GiteaCreateBranch.

The dangerous shape here is a *silent wrong success*: reporting a branch was
created when it already existed pointing somewhere else. That is the `PATCH {}`
family -- the caller gets a green result and then writes to a branch that
is not what they think it is.
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

SHA_A = "a" * 40
SHA_B = "b" * 40
REPO_INFO = {"name": "demo", "default_branch": "main", "full_name": "LLM/demo"}


def branch(name, sha, protected=False):
    return {"name": name, "commit": {"id": sha}, "protected": protected}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def post(self, **body):
        return self.client.post("/gitea/branches/LLM/demo", json=body)

    def creates(self):
        return self.fake.calls_matching("POST", "/branches")


class TestCreateNew(Base):
    def test_creates_from_default_branch_when_no_ref_given(self):
        # Specific first: "/repos/LLM/demo" is a substring of
        # "/repos/LLM/demo/branches/feat" and FakeGitea matches substrings.
        self.fake.route("GET", "/branches/feat", FakeResponse(404, {"message": "no"}))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, REPO_INFO))
        self.fake.route("POST", "/branches", FakeResponse(201, branch("feat", SHA_A)))
        r = self.post(new_branch_name="feat")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["created"])
        self.assertFalse(body["already_existed"])
        self.assertEqual(body["sha"], SHA_A)
        self.assertEqual(body["ref"], "main")
        self.assertEqual(
            self.creates()[0][2], {"new_branch_name": "feat", "old_ref_name": "main"}
        )

    def test_creates_from_explicit_ref(self):
        self.fake.route("GET", "/branches/feat", FakeResponse(404, {"message": "no"}))
        self.fake.route("POST", "/branches", FakeResponse(201, branch("feat", SHA_B)))
        r = self.post(new_branch_name="feat", old_ref_name="v1.2.0")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.creates()[0][2], {"new_branch_name": "feat", "old_ref_name": "v1.2.0"}
        )

    def test_flat_args_are_tolerated_and_not_forwarded(self):
        """Open-WebUI echoes owner/repo into the body."""
        self.fake.route("GET", "/branches/feat", FakeResponse(404, {"message": "no"}))
        self.fake.route("POST", "/branches", FakeResponse(201, branch("feat", SHA_A)))
        r = self.post(
            new_branch_name="feat", old_ref_name="main", owner="LLM", repo="demo"
        )
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.creates()[0][2]
        self.assertNotIn("owner", sent)
        self.assertNotIn("repo", sent)

    def test_typo_is_rejected_without_a_write(self):
        r = self.post(new_branch="feat")
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.creates(), [])

    def test_protection_is_reported(self):
        self.fake.route("GET", "/branches/feat", FakeResponse(404, {"message": "no"}))
        self.fake.route("POST", "/branches", FakeResponse(201, branch("feat", SHA_A, True)))
        r = self.post(new_branch_name="feat", old_ref_name="main")
        self.assertTrue(r.json()["protected"])


class TestIdempotency(Base):
    def test_existing_at_same_ref_is_not_an_error(self):
        self.fake.route("GET", "/branches/feat", FakeResponse(200, branch("feat", SHA_A)))
        self.fake.route("GET", "/branches/main", FakeResponse(200, branch("main", SHA_A)))
        r = self.post(new_branch_name="feat", old_ref_name="main")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["created"])
        self.assertTrue(body["already_existed"])
        self.assertEqual(body["sha"], SHA_A)
        self.assertEqual(self.creates(), [], "must not re-create")

    def test_existing_at_different_ref_is_a_409_with_both_shas(self):
        """The core silent-wrong-success guard."""
        self.fake.route("GET", "/branches/feat", FakeResponse(200, branch("feat", SHA_B)))
        self.fake.route("GET", "/branches/main", FakeResponse(200, branch("main", SHA_A)))
        r = self.post(new_branch_name="feat", old_ref_name="main")
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertIn(SHA_B[:12], detail)
        self.assertIn(SHA_A[:12], detail)
        self.assertEqual(self.creates(), [], "must not write")

    def test_missing_source_ref_surfaces_upstream_404(self):
        self.fake.route("GET", "/branches/nope", FakeResponse(404, {"message": "no"}))
        self.fake.route("GET", "/branches/feat", FakeResponse(404, {"message": "no"}))
        self.fake.route("POST", "/branches", FakeResponse(404, {"message": "old branch does not exist"}))
        r = self.post(new_branch_name="feat", old_ref_name="nope")
        self.assertEqual(r.status_code, 404, r.text)


class TestRoutingAndSpec(unittest.TestCase):
    def test_operation_id_shape_and_methods(self):
        spec = TestClient(app).get("/openapi.json").json()
        path = "/gitea/branches/{owner}/{repo}"
        self.assertEqual(spec["paths"][path]["post"]["operationId"], "GiteaCreateBranch")
        self.assertEqual(spec["paths"][path]["get"]["operationId"], "GiteaListBranches")
        # GET and POST deliberately share a path, pairing a read with a write.
        self.assertEqual(set(spec["paths"][path]), {"get", "post"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
