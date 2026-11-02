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
    """Records every outbound call and replays queued/derived responses."""

    def __init__(self):
        self.calls = []          # (method, url, json_body)
        self.routes = {}         # (method, url_substring) -> FakeResponse | callable
        self.default = FakeResponse(200, {})

    def route(self, method, contains, response):
        self.routes[(method.upper(), contains)] = response

    def calls_matching(self, method, contains):
        return [c for c in self.calls
                if c[0].upper() == method.upper() and contains in c[1]]

    async def _request(self, method, url, **kwargs):
        body = kwargs.get("json")
        self.calls.append((method, str(url), body))
        for (m, contains), resp in self.routes.items():
            if m == method.upper() and contains in str(url):
                return resp(url, body) if callable(resp) else resp
        return self.default

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
