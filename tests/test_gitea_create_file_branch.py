"""Tests for GiteaCreateFile's branch resolution.

The bug these pin: ``create_file`` fell back to ``branch = "main"`` both when
``default_branch`` was absent from the repo payload and when the repo read
raised. A wrong branch does not fail at resolution time -- Gitea answers 404
``branch does not exist`` on the write, which reads as a bad branch name when
the real fault is that the default was never determined.

No existing test covered that fallback, which is why replacing it broke nothing.
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

FILE = {"owner": "LLM", "repo": "demo", "path": "new.txt", "content": "hi"}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig

    def write(self, **overrides):
        body = dict(FILE)
        body.update(overrides)
        return self.client.post("/gitea/files", json=body)

    def writes(self):
        return [c for c in self.fake.calls
                if c[0] in ("PUT", "POST") and "/contents/" in c[1]]


class TestBranchResolution(Base):
    def test_explicit_branch_is_used_and_repo_not_read(self):
        self.fake.route("PUT", "/contents/new.txt", FakeResponse(201, {"content": {}}))
        self.fake.route("GET", "/contents/new.txt",
                        FakeResponse(404, None, text='{"message":"not found"}'))
        response = self.write(branch="feature/x")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("feature/x", self.writes()[0][2]["branch"])

    def test_repo_default_branch_is_used_when_omitted(self):
        # Order matters: FakeGitea matches on substrings and returns the FIRST
        # hit, so a /repos/LLM/demo route registered before /contents/new.txt
        # would also answer the contents GET and hand back a repo payload.
        self.fake.route("GET", "/contents/new.txt",
                        FakeResponse(404, None, text='{"message":"not found"}'))
        self.fake.route("POST", "/contents/new.txt", FakeResponse(201, {"content": {}}))
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, {"default_branch": "trunk"}))
        response = self.write()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("trunk", self.writes()[0][2]["branch"])

    def test_missing_default_branch_in_payload_is_refused_not_guessed(self):
        """Was: silently wrote to "main". Now: 502, nothing written."""
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, {}))
        response = self.write()
        self.assertEqual(502, response.status_code, response.text)
        self.assertEqual([], self.writes())

    def test_failed_repo_read_is_refused_not_guessed(self):
        """Was: `except GiteaError: branch = "main"`. Now: the error surfaces."""
        self.fake.route("GET", "/repos/LLM/demo",
                        FakeResponse(500, {"message": "boom"}))
        response = self.write()
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual([], self.writes())

    def test_default_branch_never_defaults_to_main(self):
        """A repo with no default_branch must not be treated as "main"."""
        self.fake.route("GET", "/repos/LLM/demo", FakeResponse(200, {"default_branch": ""}))
        response = self.write()
        self.assertEqual(502, response.status_code, response.text)
        self.assertNotIn("main", [c[2]["branch"] for c in self.writes() if c[2]])
