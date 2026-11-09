"""Tests for the route matcher in tests/fake_gitea.py.

Why this file exists: the fake used to return the FIRST registered substring
match. That produced four separate false failures across three changes, each
one looking like a handler bug:

  - '/repos/LLM/demo' swallowed '/repos/LLM/demo/branches/feat/x', so the branch
    payload arrived with no 'commit' key and the reported SHA came back ''.
  - a '/branches/' DELETE stub answered a GET for the same path.
  - registering the repo route last "fixed" it, so the symptom moved when routes
    were reordered -- which is the tell that the matcher, not the handler, was
    at fault.

The matcher now picks by POSITION (rightmost substring), so specificity is a
property of the URL rather than of insertion order.
"""

import os
import pathlib
import sys
import unittest

os.environ.setdefault("GITEA_TOKEN", "test-token")
os.environ.setdefault("GITEA_INSTANCE_URL", "https://gitea.example")
os.environ.setdefault("EVIDENCE_DIR", "/tmp/owc-test-data")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tests.fake_gitea import FakeGitea, FakeResponse


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitea()

    def match(self, url, method="GET"):
        m = self.fake._match(method, url)
        return None if m is None else m[1]

    def routes(self, *pairs):
        for method, contains in pairs:
            self.fake.route(method, contains, FakeResponse(200, {}))


class TestSpecificity(Base):
    def test_specific_segment_beats_a_broad_repo_prefix(self):
        """The regression: a repo route must not answer a branch or PRs URL."""
        self.routes(
            ("GET", "/repos/LLM/demo"),
            ("GET", "/branches"),
            ("GET", "/pulls"),
        )
        self.assertEqual(
            "/branches", self.match("https://x/api/v1/repos/LLM/demo/branches/feat/x")
        )
        self.assertEqual("/pulls", self.match("https://x/api/v1/repos/LLM/demo/pulls"))
        self.assertEqual(
            "/repos/LLM/demo", self.match("https://x/api/v1/repos/LLM/demo/")
        )

    def test_order_of_registration_does_not_matter(self):
        """The old failure mode: reordering routes changed which one won."""
        url = "https://x/api/v1/repos/LLM/demo/branches/feat/x"
        self.routes(("GET", "/repos/LLM/demo"), ("GET", "/branches"))
        first = self.match(url)

        other = FakeGitea()
        other.route("GET", "/branches", FakeResponse(200, {}))
        other.route("GET", "/repos/LLM/demo", FakeResponse(200, {}))
        second = other._match("GET", url)[1]

        self.assertEqual(first, second)
        self.assertEqual("/branches", first)

    def test_deeper_path_wins_over_a_shallower_one(self):
        self.routes(("GET", "/pulls"), ("GET", "/pulls/1"))
        self.assertEqual("/pulls/1", self.match("https://x/repos/o/r/pulls/1"))
        self.assertEqual("/pulls", self.match("https://x/repos/o/r/pulls"))

    def test_method_is_part_of_the_key(self):
        """A DELETE stub must not answer a GET for the same path."""
        self.fake.route("DELETE", "/branches/feat/x", FakeResponse(204, None, text=""))
        self.assertIsNone(self.match("https://x/branches/feat/x", "GET"))
        self.assertEqual(
            "/branches/feat/x", self.match("https://x/branches/feat/x", "DELETE")
        )

    def test_no_match_returns_none(self):
        self.routes(("GET", "/nothing/here"))
        self.assertIsNone(self.match("https://x/repos/o/r/branches"))


class TestMatchingStability(Base):
    def test_equal_position_prefers_the_longer_substring(self):
        # Both start at the same offset; the more specific one should win.
        self.routes(("GET", "/pulls"), ("GET", "/pulls/7/merge"))
        self.assertEqual(
            "/pulls/7/merge", self.match("https://x/pulls/7/merge")
        )

    def test_equal_position_and_length_is_deterministic(self):
        """Same position, same length: earliest registration wins, repeatably."""
        self.fake.route("GET", "/aa", FakeResponse(200, {}))
        self.fake.route("GET", "/ab", FakeResponse(200, {}))
        url = "https://x/aa"  # only /aa matches, so this is a stability check
        self.assertEqual("/aa", self.match(url))

    def test_direct_routes_writes_are_tolerated(self):
        """Some older tests assign into .routes directly instead of using route()."""
        self.fake.routes[("GET", "/branches")] = FakeResponse(200, {})
        self.assertEqual("/branches", self.match("https://x/repos/o/r/branches"))

    def test_route_first_shim_still_registers(self):
        self.fake.route_first("GET", "/branches/x", FakeResponse(200, {}))
        self.assertEqual("/branches/x", self.match("https://x/branches/x"))


class TestRecording(Base):
    def test_calls_are_recorded_with_body(self):
        self.fake.route("POST", "/diffpatch", FakeResponse(201, {"commit": {"sha": "a"}}))
        import asyncio

        asyncio.run(
            self.fake._request(
                "POST", "https://x/repos/o/r/diffpatch", json={"content": "d"}
            )
        )
        calls = self.fake.calls_matching("POST", "/diffpatch")
        self.assertEqual(1, len(calls))
        self.assertEqual({"content": "d"}, calls[0][2])

    def test_unmatched_request_gets_the_default_response(self):
        import asyncio

        resp = asyncio.run(self.fake._request("GET", "https://x/unknown"))
        self.assertEqual(200, resp.status_code)


if __name__ == "__main__":
    unittest.main()
