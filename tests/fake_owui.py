"""Dependency-free fake of Open-WebUI for app.notes_sync.

Patches ``httpx.AsyncClient`` so the sync can be driven in-process with no
network, no OWUI instance and no pytest dependency. It models the parts of OWUI
that the notes sync actually depends on, including two behaviours that are easy
to get wrong and were real bugs:

* ``GET /notes/`` truncates note content to 1000 characters, while
  ``GET /notes/{id}`` does not. A fake that returned full content from the list
  would hide the truncation bug entirely.
* ``POST /files/`` only links the file to the knowledge base when
  ``process_in_background=false``. With the default the file comes back
  ``pending`` and does not appear in the knowledge base at all.

It also enforces the payload shapes OWUI enforces (SyncDiffForm rejects extra
keys; ``dirs/create`` and ``file/remove`` need specific bodies), so a regression
in our request building fails here rather than in production.
"""

import json
import re

TRUNCATE_AT = 1000


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode()

    def json(self):
        # Mirrors httpx: a non-JSON body raises JSONDecodeError rather than
        # returning a sentinel.
        return json.loads(self.content)

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("GET", "http://fake"),
                response=httpx.Response(self.status_code, text=self.text),
            )


def _error(status, detail):
    return FakeResponse(status, {"detail": detail})


class FakeOWUI:
    """An in-memory Open-WebUI: notes, one knowledge base, its files and dirs."""

    def __init__(self, kb_id="kb-1", kb_name="Notes Sync"):
        self.kb_id = kb_id
        self.kb_name = kb_name
        self.notes = {}           # note_id -> full note dict
        self.files = {}           # file_id -> {filename, directory_id, data}
        self.directories = {}     # dir_id -> {name, parent_id}
        self.calls = []           # (method, url, kwargs-summary)
        self.fail_upload_of = set()   # filenames whose upload returns 500
        self.fail_diff = False
        self._seq = 0

    # ── fixtures ──────────────────────────────────────────────────────────

    def add_note(self, note_id, title, md, pages=None):
        page_list = pages or [
            {
                "id": "main",
                "title": title,
                "content": {"md": md},
            }
        ]
        self.notes[note_id] = {
            "id": note_id,
            "title": title,
            # Legacy mirror, as OWUI stores it.
            "data": {
                "content": {"md": md},
                "pages": page_list,
                "page_count": len(page_list),
            },
        }
        return note_id

    def _next_id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}"

    # ── helpers ───────────────────────────────────────────────────────────

    def _path_of(self, directory_id):
        names = []
        seen = set()
        current = directory_id
        while current and current in self.directories and current not in seen:
            seen.add(current)
            names.append(self.directories[current]["name"])
            current = self.directories[current]["parent_id"]
        return "/".join(reversed(names))

    def _id_for_path(self, path):
        for directory_id in self.directories:
            if self._path_of(directory_id) == path:
                return directory_id
        return None

    def _file_hash(self, file_id):
        return self.files[file_id]["data"].get("file_hash")

    def files_in_kb(self):
        return sorted(
            (f["filename"], self._path_of(f["directory_id"]))
            for f in self.files.values()
        )

    def calls_matching(self, method, contains):
        return [
            c
            for c in self.calls
            if c[0].upper() == method.upper() and contains in c[1]
        ]

    # ── routes ────────────────────────────────────────────────────────────

    def _diff(self, body):
        if self.fail_diff:
            return _error(500, "diff exploded")

        manifest = body.get("manifest")
        if not isinstance(manifest, list):
            return _error(422, "manifest is required")

        allowed = {"filename", "path", "checksum", "size"}
        for entry in manifest:
            extra = set(entry) - allowed
            if extra:
                # SyncDiffForm forbids extra keys: this is the 422 that made
                # every sync fail when the manifest carried `content`.
                return _error(422, f"Extra inputs are not permitted: {sorted(extra)}")
            if "path" not in entry:
                return _error(422, "path is required")

        indexed = {}
        for file_id, file in self.files.items():
            key = (self._path_of(file["directory_id"]), file["filename"])
            indexed[key] = (file_id, self._file_hash(file_id))

        added, modified, deleted = [], [], []
        keys = set()
        unmodified = 0
        for entry in manifest:
            key = (entry["path"], entry["filename"])
            keys.add(key)
            current = indexed.get(key)
            if current is None:
                added.append({"filename": entry["filename"], "path": entry["path"]})
            elif current[1] != entry["checksum"]:
                modified.append(
                    {
                        "filename": entry["filename"],
                        "path": entry["path"],
                        "stale_file_id": current[0],
                    }
                )
            else:
                unmodified += 1

        for key, (file_id, _checksum) in indexed.items():
            if key not in keys:
                deleted.append({"file_id": file_id, "filename": key[1]})

        required = set()
        for entry in manifest:
            if entry["path"]:
                segments = entry["path"].split("/")
                for depth in range(len(segments)):
                    required.add("/".join(segments[: depth + 1]))

        existing = {self._path_of(d) for d in self.directories}
        mkdir = sorted(required - existing, key=lambda p: p.count("/"))
        rmdir = [
            d for d in self.directories if self._path_of(d) not in required
        ]

        return FakeResponse(
            200,
            {
                "added": added,
                "modified": modified,
                "deleted": deleted,
                "mkdir": mkdir,
                "rmdir": rmdir,
                "unmodified_count": unmodified,
                "directory_map": {
                    self._path_of(d): d for d in self.directories
                },
            },
        )

    def _dirs_create(self, body):
        name = body.get("name")
        parent_id = body.get("parent_id")
        if not name:
            return _error(422, "name is required")
        if parent_id and parent_id not in self.directories:
            return _error(404, "Parent directory not found.")
        new_id = self._next_id("dir")
        self.directories[new_id] = {"name": name, "parent_id": parent_id}
        return FakeResponse(
            200,
            {
                "id": new_id,
                "knowledge_id": self.kb_id,
                "parent_id": parent_id,
                "name": name,
            },
        )

    def _upload(self, url, kwargs):
        params = kwargs.get("params") or {}
        files = kwargs.get("files") or {}
        data = kwargs.get("data") or {}

        headers = kwargs.get("headers") or {}
        content_type = headers.get("Content-Type") or headers.get("content-type")
        if content_type and "multipart" not in content_type:
            # A JSON content-type on a multipart post is the bug this guards.
            return _error(422, "expected multipart/form-data")

        if "file" not in files:
            return _error(422, "file is required")
        filename, payload, _mime = files["file"]

        if filename in self.fail_upload_of:
            return _error(500, f"embedding failed for {filename}")

        metadata = data.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        metadata = metadata or {}

        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        if not text.strip():
            return _error(400, "The content provided is empty.")

        file_id = self._next_id("file")
        directory_id = metadata.get("directory_id")
        background = str(params.get("process_in_background", "true")).lower() != "false"

        if not background and metadata.get("knowledge_id"):
            # Linked to the KB, as OWUI does in-request.
            linked = True
        else:
            linked = False

        import hashlib

        self.files[file_id] = {
            "filename": filename,
            "directory_id": directory_id if linked else None,
            "linked": linked,
            "data": {"file_hash": hashlib.sha256(payload).hexdigest()},
        }
        if not linked:
            # Unlinked files are invisible to /knowledge/{id}/files and to the
            # diff, so a background upload silently re-uploads forever.
            self.files[file_id]["directory_id"] = None

        return FakeResponse(
            200,
            {
                "status": True,
                "id": file_id,
                "filename": filename,
                "data": {"status": "pending" if background else "completed"},
                "meta": {"data": metadata},
            },
        )

    def _file_remove(self, body):
        file_id = body.get("file_id")
        if file_id not in self.files:
            return _error(400, "not found")
        del self.files[file_id]
        return FakeResponse(200, {"id": self.kb_id})

    def _cleanup(self, body):
        removed = 0
        for file_id in body.get("file_ids") or []:
            if file_id in self.files:
                del self.files[file_id]
                removed += 1
        for dir_id in body.get("dir_ids") or []:
            self.directories.pop(dir_id, None)
        return FakeResponse(200, {"status": True, "removed": removed})

    def _list_files(self, kwargs):
        params = kwargs.get("params") or {}
        directory_id = params.get("directory_id")
        items = []
        for file_id, file in self.files.items():
            if directory_id and file["directory_id"] != directory_id:
                continue
            items.append(
                {
                    "id": file_id,
                    "filename": file["filename"],
                    "directory_id": file["directory_id"],
                    "directory_path": self._path_of(file["directory_id"]),
                }
            )
        return FakeResponse(
            200,
            {
                "items": items,
                "directories": [],
                "breadcrumbs": [],
                "total": len(items),
            },
        )

    def _handle(self, method, url, kwargs):
        method = method.upper()
        calls = self.calls.append(
            (
                method,
                str(url),
                {
                    "params": kwargs.get("params"),
                    "files": list((kwargs.get("files") or {}).keys()),
                    "data": kwargs.get("data"),
                },
            )
        )
        assert calls is None

        path = re.sub(r"^https?://[^/]+", "", str(url))
        path = path.split("?")[0]

        body = kwargs.get("json") or {}

        # Notes
        if method == "GET" and path.rstrip("/").endswith("/notes"):
            listing = []
            for note in self.notes.values():
                # OWUI truncates the LIST response to 1000 chars.
                md = (note["data"].get("content") or {}).get("md") or ""
                listing.append(
                    {
                        **note,
                        "data": {
                            "content": {"md": md[:TRUNCATE_AT]},
                            "page_count": note["data"].get("page_count", 1),
                        },
                    }
                )
            return FakeResponse(200, listing)

        match = re.search(r"/notes/([^/]+)$", path)
        if method == "GET" and match:
            note = self.notes.get(match.group(1))
            if not note:
                return _error(404, "not found")
            return FakeResponse(200, note)

        # Knowledge base
        match = re.search(rf"/knowledge/{re.escape(self.kb_id)}$", path)
        if method == "GET" and match:
            return FakeResponse(200, {"id": self.kb_id, "name": self.kb_name})

        if path.endswith("/sync/diff") and method == "POST":
            return self._diff(body)
        if path.endswith("/dirs/create") and method == "POST":
            return self._dirs_create(body)
        if path.endswith("/file/remove") and method == "POST":
            return self._file_remove(body)
        if path.endswith("/sync/cleanup") and method == "POST":
            return self._cleanup(body)
        if path.endswith("/files") and method == "GET":
            return self._list_files(kwargs)
        if re.search(r"/files/?$", path) and method == "POST":
            return self._upload(url, kwargs)

        return _error(404, f"unhandled {method} {path}")

    def install(self, monkeypatch_target):
        """Return a class to monkeypatch in place of httpx.AsyncClient."""
        outer = self

        class _Client:
            def __init__(self, *a, **kw):
                self._timeout = kw.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, **kwargs):
                return outer._handle(method, url, kwargs)

            async def get(self, url, **kwargs):
                return outer._handle("GET", url, kwargs)

            async def post(self, url, **kwargs):
                return outer._handle("POST", url, kwargs)

        monkeypatch_target.AsyncClient = _Client
        return self
