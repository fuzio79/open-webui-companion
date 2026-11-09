"""Dependency-free fake of the Gitea upstream used by app.gitea.

Patches httpx.AsyncClient so the FastAPI app can be driven in-process with no
network, no credentials and no respx/pytest dependency.
"""

import json


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode()

    def json(self):
        # Mirrors httpx: decoding an empty or non-JSON body raises
        # JSONDecodeError instead of returning a sentinel, otherwise the
        # 204-on-success bug this fake exists to catch cannot reproduce.
        return json.loads(self.content)

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("GET", "http://fake"),
                response=httpx.Response(self.status_code, text=self.text),
            )


class FakeGitea:
    """Records every outbound call and replays queued/derived responses.

    ROUTE MATCHING: routes are keyed on a URL *substring*, and the match whose
    substring appears LATEST in the URL wins (ties go to the longer substring,
    then to the earliest registered).

    This matters more than it looks. The original implementation returned the
    first registered match, which is insertion-order dependent and silently
    matched partial path segments:

        registered: GET /repos/LLM/demo
        request:    GET /repos/LLM/demo/branches/feat/x
        -> returned the repo payload (no 'commit' key)

    That is a fake-side error that looks exactly like a handler bug: a field
    silently comes back empty. It cost debugging time in three separate
    changes, so specificity is now explicit rather than an accident of
    ordering, and tests/test_fake_gitea.py pins the behaviour.

    Why *rightmost* and not *longest*: '/repos/LLM/demo' is 15 characters and
    '/branches' is 9, yet for '/repos/LLM/demo/branches' the branches route is
    the specific one. Length alone ranks the broad prefix higher. The substring
    that starts latest in the URL is the one closest to the requested resource.
    """

    def __init__(self):
        self.calls = []          # (method, url, json_body)
        self.routes = {}         # (method, url_substring) -> FakeResponse | callable
        self.default = FakeResponse(200, {})
        self._order = {}         # (method, substring) -> registration index
        self._seq = 0

    def route(self, method, contains, response):
        key = (method.upper(), contains)
        # Re-registering the same key replaces it and counts as a later entry.
        self.routes[key] = response
        self._order[key] = self._seq
        self._seq += 1

    def route_first(self, method, contains, response):
        """Kept for readability, no longer required for precedence.

        Precedence is positional (see `_match`), so a specific route beats a
        broad one regardless of registration order. This exists so older tests
        that call it still read correctly; it is now just `route`.
        """
        self.route(method, contains, response)

    def calls_matching(self, method, contains):
        return [c for c in self.calls
                if c[0].upper() == method.upper() and contains in c[1]]

    def _match(self, method, url):
        """Return the most specific matching route, or None.

        Specificity is the RIGHTMOST occurrence of the substring: for
        '/repos/LLM/demo/branches', '/branches' starts later than
        '/repos/LLM/demo', so it wins even though it is shorter. Ties go to the
        longer substring, then to the earliest registration, which keeps a
        broad route from shadowing a specific one registered after it.
        """
        method = method.upper()
        url = str(url)
        best = None
        best_key = None
        for (m, contains) in self.routes:
            if m != method:
                continue
            at = url.find(contains)
            if at < 0:
                continue
            key = (at, len(contains), -self._order.get((m, contains), 0))
            if best_key is None or key > best_key:
                best_key = key
                best = (m, contains)
        return best

    async def _request(self, method, url, **kwargs):
        body = kwargs.get("json")
        self.calls.append((method, str(url), body))
        match = self._match(method, url)
        if match is None:
            return self.default
        resp = self.routes[match]
        return resp(url, body) if callable(resp) else resp

    def install(self, monkeypatch_target):
        """Return a class to monkeypatch in place of httpx.AsyncClient."""
        outer = self

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, **kwargs):
                return await outer._request(method, url, **kwargs)

            async def get(self, url, **kwargs):
                return await outer._request("GET", url, **kwargs)

        monkeypatch_target.AsyncClient = _Client
        return self
