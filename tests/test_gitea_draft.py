"""Tests for translating `draft` into the title prefix Gitea actually honours.

Gitea 28.1.0's CreatePullRequestOption and EditPullRequestOption have no `draft`
property, so sending one was silently discarded: the call returned 201 on a
normal, mergeable PR. Nothing in the response revealed the difference -- the
same failure shape as a `PATCH {}` that succeeds while changing nothing.

Gitea derives draft-ness from the TITLE, verified live:

    "WIP: x" "WIP:" "[WIP] x" "wip: x" "[wip] x" "  WIP: x"  -> draft
    "WIP x"  "(WIP) x" "Draft: x"                            -> not a draft

and a WIP PR is refused a merge with 405 "Work in progress PRs cannot be
merged", so the prefix is a real guard rather than cosmetic.

The fake cannot reproduce any of that: it matches on URL substrings and knows
nothing about Gitea's title heuristics. These tests therefore pin *what we send*
(and that we stop sending the dead field); the prefix semantics themselves are
covered by a live check recorded in the issue.
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

from app.gitea import _apply_draft, _has_wip_prefix, _strip_wip_prefix
from app.main import app
from tests.fake_gitea import FakeGitea, FakeResponse

PR = {
    "number": 5,
    "title": "Add sprocket",
    "state": "open",
    "merged": False,
    "draft": False,
    "html_url": "https://gitea.example/LLM/demo/pulls/5",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()
        self._orig = httpx.AsyncClient
        self.fake.install(httpx)
        self.client = TestClient(app)

    def tearDown(self):
        httpx.AsyncClient = self._orig


def created_payload(fake):
    calls = fake.calls_matching("POST", "/pulls")
    assert calls, "no PR create call was made"
    return calls[0][2]


class TestApplyDraft(unittest.TestCase):
    """The translation itself. Pure functions, so these are exact."""

    def test_draft_true_adds_prefix(self):
        self.assertEqual(_apply_draft("Add sprocket", True), "WIP: Add sprocket")

    def test_draft_false_removes_prefix(self):
        self.assertEqual(_apply_draft("WIP: Add sprocket", False), "Add sprocket")

    def test_draft_true_is_idempotent(self):
        """Asking twice must not produce "WIP: WIP: ..."."""
        once = _apply_draft("Add sprocket", True)
        self.assertEqual(_apply_draft(once, True), once)

    def test_bracket_form_is_stripped_too(self):
        """[WIP] is a documented prefix, so it must not survive un-drafting."""
        self.assertEqual(_apply_draft("[WIP] Add sprocket", False), "Add sprocket")

    def test_lowercase_form_is_stripped(self):
        self.assertEqual(_apply_draft("wip: Add sprocket", False), "Add sprocket")

    def test_leading_whitespace_is_tolerated(self):
        self.assertEqual(_apply_draft("  WIP: Add", False), "Add")

    def test_no_colon_is_not_treated_as_a_prefix(self):
        """'WIP Add' is NOT a draft upstream, so it must not be stripped."""
        self.assertEqual(_apply_draft("WIP Add sprocket", False), "WIP Add sprocket")

    def test_has_wip_prefix(self):
        for title in ("WIP: x", "[WIP] x", "wip: x", "  WIP:x"):
            self.assertTrue(_has_wip_prefix(title), title)
        for title in ("WIP x", "(WIP) x", "Draft: x", "Add WIP: later"):
            self.assertFalse(_has_wip_prefix(title), title)

    def test_strip_leaves_mid_title_markers_alone(self):
        self.assertEqual(_strip_wip_prefix("Fix WIP: handling"), "Fix WIP: handling")


class TestCreatePRDraft(Base):
    def test_draft_is_not_sent_as_a_dead_field(self):
        """The whole bug: `draft` was accepted by Gitea and discarded."""
        self.fake.route("POST", "/pulls", FakeResponse(201, dict(PR, draft=True,
                                                                title="WIP: Add sprocket")))
        self.client.post(
            "/gitea/pulls",
            json={"owner": "LLM", "repo": "demo", "title": "Add sprocket",
                  "head": "feat", "base": "main", "draft": True},
        )
        sent = created_payload(self.fake)
        self.assertNotIn("draft", sent, "Gitea ignores this field; sending it is a lie")

    def test_draft_true_sends_a_wip_title(self):
        self.fake.route("POST", "/pulls", FakeResponse(201, dict(PR, draft=True,
                                                                title="WIP: Add sprocket")))
        self.client.post(
            "/gitea/pulls",
            json={"owner": "LLM", "repo": "demo", "title": "Add sprocket",
                  "head": "feat", "base": "main", "draft": True},
        )
        self.assertEqual(created_payload(self.fake)["title"], "WIP: Add sprocket")

    def test_draft_false_leaves_title_alone(self):
        self.fake.route("POST", "/pulls", FakeResponse(201, PR))
        self.client.post(
            "/gitea/pulls",
            json={"owner": "LLM", "repo": "demo", "title": "Add sprocket",
                  "head": "feat", "base": "main"},
        )
        self.assertEqual(created_payload(self.fake)["title"], "Add sprocket")

    def test_response_reports_stored_draft_state(self):
        """Report Gitea's value, not the requested one."""
        self.fake.route("POST", "/pulls", FakeResponse(201, dict(PR, draft=True,
                                                                title="WIP: Add sprocket")))
        r = self.client.post(
            "/gitea/pulls",
            json={"owner": "LLM", "repo": "demo", "title": "Add sprocket",
                  "head": "feat", "base": "main", "draft": True},
        )
        body = r.json()
        self.assertTrue(body["draft"])
        self.assertTrue(body["draft_requested"])
        self.assertEqual(body["draft_applied_via"], "title_prefix")
        self.assertEqual(body["title"], "WIP: Add sprocket", "final title is visible")

    def test_a_dropped_draft_is_visible_not_silent(self):
        """If a Gitea version ignored the prefix, the caller must still see it.

        Without this, a version change would silently restore the original bug.
        """
        self.fake.route("POST", "/pulls", FakeResponse(201, dict(PR, draft=False)))
        r = self.client.post(
            "/gitea/pulls",
            json={"owner": "LLM", "repo": "demo", "title": "Add sprocket",
                  "head": "feat", "base": "main", "draft": True},
        )
        body = r.json()
        self.assertTrue(body["draft_requested"])
        self.assertFalse(body["draft"], "stored state wins over requested state")


class TestUpdatePRDraft(Base):
    def test_patch_to_draft_reads_current_title_then_prefixes(self):
        self.fake.route("GET", "/pulls/5", FakeResponse(200, PR))
        self.fake.route("PATCH", "/pulls/5",
                        FakeResponse(200, dict(PR, draft=True, title="WIP: Add sprocket")))
        r = self.client.patch("/gitea/pulls/LLM/demo/5", json={"draft": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.fake.calls_matching("PATCH", "/pulls/5")[0][2],
            {"title": "WIP: Add sprocket"},
        )

    def test_patch_clears_draft(self):
        """Reversibility: the same field must un-draft."""
        self.fake.route("GET", "/pulls/5",
                        FakeResponse(200, dict(PR, draft=True, title="WIP: Add sprocket")))
        self.fake.route("PATCH", "/pulls/5", FakeResponse(200, dict(PR, draft=False)))
        self.client.patch("/gitea/pulls/LLM/demo/5", json={"draft": False})
        self.assertEqual(
            self.fake.calls_matching("PATCH", "/pulls/5")[0][2], {"title": "Add sprocket"}
        )

    def test_patch_uses_supplied_title_when_given(self):
        """No GET needed if the caller already says what the title should be."""
        self.fake.route("PATCH", "/pulls/5",
                        FakeResponse(200, dict(PR, draft=True, title="WIP: New name")))
        self.client.patch(
            "/gitea/pulls/LLM/demo/5", json={"title": "New name", "draft": True}
        )
        self.assertEqual(
            self.fake.calls_matching("PATCH", "/pulls/5")[0][2],
            {"title": "WIP: New name"},
        )

    def test_draft_alone_is_not_rejected_as_empty(self):
        """The empty-payload guard must count `draft` as a real field."""
        self.fake.route("GET", "/pulls/5", FakeResponse(200, PR))
        self.fake.route("PATCH", "/pulls/5", FakeResponse(200, dict(PR, draft=True)))
        r = self.client.patch("/gitea/pulls/LLM/demo/5", json={"draft": True})
        self.assertNotEqual(r.status_code, 422, r.text)

    def test_empty_body_still_rejected(self):
        r = self.client.patch("/gitea/pulls/LLM/demo/5", json={})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.fake.calls_matching("PATCH", "/pulls/5"), [])

    def test_null_draft_leaves_title_untouched(self):
        """`null` must mean 'leave it alone', not 'un-draft'."""
        self.fake.route("GET", "/pulls/5", FakeResponse(200, dict(PR, title="WIP: x")))
        self.fake.route("PATCH", "/pulls/5", FakeResponse(200, PR))
        self.client.patch("/gitea/pulls/LLM/demo/5", json={"draft": None, "body": "hi"})
        self.assertEqual(
            self.fake.calls_matching("PATCH", "/pulls/5")[0][2], {"body": "hi"}
        )


class TestDraftInOpenAPI(unittest.TestCase):
    """Open-WebUI builds its tool schema from this."""

    def setUp(self):
        self.spec = TestClient(app).get("/openapi.json").json()

    def _props(self, name):
        return set(self.spec["components"]["schemas"][name]["properties"])

    def test_create_advertises_draft(self):
        self.assertIn("draft", self._props("PRCreate"))

    def test_update_advertises_draft(self):
        self.assertIn("draft", self._props("PRUpdate"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
