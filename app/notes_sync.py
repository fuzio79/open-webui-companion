"""Notes-to-Knowledge-Base sync for Open-WebUI.

Fetches notes from Open-WebUI and pushes them into a Knowledge Base for
automatic RAG indexing.

Two things about the OWUI API shape are easy to get wrong and both are handled
here:

* ``GET /notes/`` returns note content **truncated to 1000 characters**
  (``_truncate_note_data`` in ``routers/notes.py``). The list endpoint is
  therefore only used to discover ids; the full body comes from
  ``GET /notes/{id}``.
* Content lives per *page* (``data.pages[].content.md``), with a legacy
  single-page mirror at ``data.content.md``. A note can have several pages, so
  one note may produce several KB files.
"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.errors import ConfigError, OWUIError
from app.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/notes-sync", tags=["notes-sync"])

#: Virtual directory inside the knowledge base that this service owns. Deletion
#: is scoped to it -- the sync diff reports every file it does not know about,
#: including documents that were never ours.
_NOTES_DIR = "notes"

_EXT = ".md"
_MAX_NAME = 120
_MAX_STEM = _MAX_NAME - len(_EXT)
_UPLOAD_MIME = "text/markdown"
_MANIFEST_FILENAME = "notes_sync_manifest.json"


class SyncStatus:
    """Track the last sync state for incremental updates."""

    def __init__(self):
        self.last_sync: Optional[str] = None
        self.last_count: int = 0
        self.last_error: Optional[str] = None
        # "path/filename" -> sha256 of the bytes we uploaded. Persisted so a
        # container restart does not force a full re-upload.
        self.files: dict = {}
        self._loaded: bool = False

    @property
    def manifest(self) -> dict:
        """Backwards-compatible alias for the checksum map."""
        return self.files

    @manifest.setter
    def manifest(self, value) -> None:
        self.files = value or {}


sync_status = SyncStatus()


# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------


def _manifest_path() -> str:
    return os.path.join(settings.EVIDENCE_DIR, _MANIFEST_FILENAME)


def _load_manifest() -> dict:
    """Read the persisted checksum map. A missing/corrupt file is not fatal."""
    path = _manifest_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - never fail startup over bookkeeping
        logger.warning("Could not read notes sync manifest", path=path, error=str(e))
        return {}

    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(k): str(v) for k, v in files.items()}


def _save_manifest(files: dict) -> None:
    """Atomically persist the checksum map next to evidence.json."""
    path = _manifest_path()
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".notes-manifest-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "files": files,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                handle,
            )
        os.replace(tmp, path)
        tmp = None
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        logger.warning("Could not persist notes sync manifest", path=path, error=str(e))
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _ensure_sync_state() -> None:
    """Load persisted state once, lazily."""
    if not sync_status._loaded:
        sync_status.files = _load_manifest()
        sync_status._loaded = True


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _owui_request(
    method: str, path: str, timeout: float = 30.0, **kwargs
) -> dict:
    """Make a JSON request to the Open-WebUI API with error handling."""
    settings.require_owui_config()

    url = f"{settings.owui_api_url}{path}"
    kwargs.setdefault("headers", settings.owui_headers)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500]
        logger.error(
            "Open-WebUI API request failed",
            method=method,
            path=path,
            status_code=e.response.status_code,
            response=error_body,
        )
        raise OWUIError(
            f"Open-WebUI API error {e.response.status_code}: {error_body}",
            status_code=e.response.status_code,
        ) from e
    except httpx.RequestError as e:
        logger.error(
            "Open-WebUI API request error", method=method, path=path, error=str(e)
        )
        raise OWUIError(f"Failed to connect to Open-WebUI: {e}") from e


def _multipart_headers() -> dict:
    """Auth-only headers for multipart uploads.

    ``settings.owui_headers`` sets ``Content-Type: application/json``. Reusing
    it for a multipart post suppresses the boundary httpx would generate, and
    OWUI answers the malformed body with a 422/400 instead of reading the file.
    """
    return {
        "Authorization": f"Bearer {settings.OWUI_TOKEN}",
        "Accept": "application/json",
    }


async def _owui_upload_file(
    kb_id: str,
    directory_id: Optional[str],
    filename: str,
    content: str,
) -> dict:
    """Upload one file's bytes and have OWUI link it to the knowledge base.

    ``metadata.knowledge_id`` makes OWUI process, embed and link the file in the
    same request. ``process_in_background=false`` matters: with the default the
    route returns a ``pending`` file before the background task links it, and
    the file is then invisible to ``sync/diff`` (it would be re-uploaded on
    every run).
    """
    settings.require_owui_config()

    url = f"{settings.owui_api_url}/files/"
    metadata = {"knowledge_id": kb_id}
    if directory_id:
        metadata["directory_id"] = directory_id

    files = {"file": (filename, content.encode("utf-8"), _UPLOAD_MIME)}
    data = {"metadata": json.dumps(metadata)}
    params = {"process": "true", "process_in_background": "false"}

    try:
        async with httpx.AsyncClient(
            timeout=settings.NOTES_SYNC_UPLOAD_TIMEOUT
        ) as client:
            response = await client.post(
                url,
                files=files,
                data=data,
                params=params,
                headers=_multipart_headers(),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500]
        logger.error(
            "File upload failed",
            filename=filename,
            status_code=e.response.status_code,
            response=error_body,
        )
        raise OWUIError(
            f"Upload of {filename!r} failed ({e.response.status_code}): {error_body}",
            status_code=e.response.status_code,
        ) from e
    except httpx.RequestError as e:
        logger.error("File upload request error", filename=filename, error=str(e))
        raise OWUIError(f"Failed to upload {filename!r}: {e}") from e


# ---------------------------------------------------------------------------
# Note fetching
# ---------------------------------------------------------------------------


async def _fetch_note_list() -> list:
    """Fetch the note index.

    Tries multiple known endpoints since the notes API path has changed across
    Open-WebUI versions. Trailing-slash forms come first: the SPA catch-all
    answers no-slash paths with HTML, which is not JSON.

    The content in this response is truncated to 1000 characters -- use it for
    ids only.
    """
    # Checked up front rather than per attempt: without a token every one of the
    # endpoints below fails identically, and swallowing that produced the
    # misleading "notes API may not be publicly accessible" message.
    settings.require_owui_config()

    endpoints = [
        "/notes/",
        "/api/v1/notes/",
        "/notes",
        "/api/v1/notes",
        "/api/notes/",
        "/api/notes",
        "/api/v1/workspace/notes",
    ]

    for path in endpoints:
        try:
            response = await _owui_request("GET", path)
            # Handle various response formats
            if isinstance(response, list):
                logger.info(
                    "Notes fetched successfully", endpoint=path, count=len(response)
                )
                return response
            if isinstance(response, dict):
                if "results" in response:
                    return response["results"]
                if "notes" in response:
                    return response["notes"]
                if "items" in response:
                    return response["items"]
                # Single note or empty dict
                if response:
                    return [response]
            logger.debug(
                "Endpoint returned non-note data",
                endpoint=path,
                type=type(response).__name__,
            )
        except ConfigError:
            raise
        except OWUIError as e:
            logger.debug("Endpoint failed", endpoint=path, error=str(e))
            continue
        except Exception as e:  # noqa: BLE001 - try the next candidate
            logger.debug("Endpoint error", endpoint=path, error=str(e))
            continue

    logger.error("Failed to fetch notes from any known endpoint", endpoints=endpoints)
    raise OWUIError(
        "Failed to fetch notes from any known endpoint. "
        "Open-WebUI's notes API may not be publicly accessible. "
        "Consider exporting notes manually or using the /notes-sync/debug endpoint to troubleshoot."
    )


async def _fetch_full_note(item: dict) -> dict:
    """Return a note with untruncated content.

    Falls back to the (truncated) list copy if the detail call fails, which is
    better than dropping the note entirely -- but it is logged, because that
    fallback silently caps the note at 1000 characters.
    """
    note_id = item.get("id") or item.get("_id")
    if not note_id:
        return item

    try:
        detail = await _owui_request("GET", f"/notes/{note_id}")
    except OWUIError as e:
        logger.warning(
            "Could not fetch full note, falling back to truncated list copy",
            note_id=note_id,
            error=str(e),
        )
        return item

    if isinstance(detail, dict) and detail.get("id"):
        return detail

    logger.warning("Note detail unusable, using list copy", note_id=note_id)
    return item


async def _fetch_notes() -> list:
    """Fetch all notes from Open-WebUI, with full content."""
    listing = await _fetch_note_list()
    if not listing:
        return []

    concurrency = max(1, settings.NOTES_SYNC_FETCH_CONCURRENCY)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(item: dict) -> dict:
        async with semaphore:
            return await _fetch_full_note(item)

    return list(await asyncio.gather(*(one(item) for item in listing)))


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


def _page_markdown(content) -> str:
    """Pull markdown text out of a page/legacy content object."""
    if isinstance(content, dict):
        for key in ("md", "markdown", "text"):
            value = content.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(content, str):
        return content
    return ""


def _note_pages(note: dict) -> list:
    """Return ``[(page_id, page_title, markdown)]`` with the main page first."""
    data = note.get("data")
    title = note.get("title") or "untitled"
    pages_out: list = []

    if isinstance(data, dict):
        raw_pages = data.get("pages")
        if isinstance(raw_pages, list) and raw_pages:
            for page in raw_pages:
                if not isinstance(page, dict):
                    continue
                pages_out.append(
                    (
                        page.get("id") or "main",
                        page.get("title") or title,
                        _page_markdown(page.get("content")),
                    )
                )
            # Stable sort: main moves to the front, subpages keep their order.
            pages_out.sort(key=lambda p: 0 if p[0] == "main" else 1)

        if not pages_out:
            md = _page_markdown(data.get("content"))
            if not md and isinstance(data.get("md"), str):
                md = data["md"]
            pages_out.append(("main", title, md))
    else:
        for key in ("content", "text", "markdown"):
            value = note.get(key)
            if isinstance(value, str):
                pages_out.append(("main", title, value))
                break

    return pages_out


def _note_content(note: dict) -> str:
    """Markdown of the note's main page (legacy single-content accessor)."""
    pages = _note_pages(note)
    for page_id, _title, md in pages:
        if page_id == "main":
            return md
    return pages[0][2] if pages else ""


def _safe_filename(title: str) -> str:
    """Make a note title safe as a knowledge-base filename stem."""
    name = re.sub(r'[\\\\/:*?"<>|]', " ", title or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return (name or "untitled")[:_MAX_STEM]


def _disambiguate_filenames(entries: list) -> None:
    """Give colliding stems a deterministic suffix.

    Two notes sharing a title would otherwise map to the same
    ``(path, filename)`` key and overwrite each other in the KB -- and the diff
    would keep flip-flopping between them. Subpages of one note never collide
    with the main page because their stem embeds the page title, but two
    subpages can, so the suffix prefers the page id when there is one.
    """
    groups: dict = {}
    for entry in entries:
        groups.setdefault((entry["path"], entry["base"]), []).append(entry)

    for group in groups.values():
        if len(group) < 2:
            continue
        for entry in group:
            page_id = entry.get("page_id") or "main"
            # The note id alone is not enough for subpages: they share it.
            tag = str(entry["note_id"])[:8]
            if page_id != "main":
                tag = f"{tag}-{str(page_id)[:8]}"
            suffix = f" ({tag}){_EXT}"
            room = _MAX_NAME - len(suffix)
            entry["filename"] = f"{entry['base'][:room].strip()}{suffix}"
            entry["base"] = entry["filename"][: -len(_EXT)]


def _wire_manifest(manifest: list) -> list:
    """Strip local-only keys so the payload matches SyncDiffForm exactly."""
    return [
        {
            "filename": i["filename"],
            "path": i["path"],
            "checksum": i["checksum"],
            "size": i["size"],
        }
        for i in manifest
    ]


async def _build_manifest(notes: list) -> list:
    """Build a manifest matching OWUI's SyncDiffForm contract.

    Every wire entry must be exactly {filename, path, checksum, size}: the route
    validates against a model that REQUIRES ``path`` and rejects extra keys,
    which is what produced a 422 on every sync.
    """
    manifest: list = []

    for note in notes:
        note_id = str(note.get("id") or note.get("_id") or "unknown")
        note_title = note.get("title") or f"note_{note_id}"

        for page_id, page_title, md in _note_pages(note):
            if not md or not md.strip():
                logger.info("Skipping empty note page", note_id=note_id, page=page_id)
                continue

            if page_id == "main":
                base = _safe_filename(note_title)
            else:
                base = _safe_filename(f"{note_title} \u2014 {page_title}")

            encoded = md.encode("utf-8")
            manifest.append(
                {
                    "filename": f"{base}{_EXT}",
                    "base": base,
                    "path": _NOTES_DIR,
                    "checksum": hashlib.sha256(encoded).hexdigest(),
                    "size": len(encoded),
                    # Local-only fields, stripped by _wire_manifest.
                    "content": md,
                    "note_id": note_id,
                    "page_id": page_id,
                }
            )

    _disambiguate_filenames(manifest)
    return manifest


# ---------------------------------------------------------------------------
# Knowledge base writes
# ---------------------------------------------------------------------------


def _entry_key(entry: dict) -> str:
    return f"{entry['path']}/{entry['filename']}"


def _in_notes_dir(path: str) -> bool:
    return path == _NOTES_DIR or path.startswith(_NOTES_DIR + "/")


async def _ensure_directories(kb_id: str, paths: list, directory_map: dict) -> dict:
    """Create missing KB directories, shallowest first so parents exist."""
    mapping = dict(directory_map or {})
    created: list = []

    for path in sorted(paths, key=lambda p: (p.count("/"), p)):
        if not path or path in mapping:
            continue
        parent_path, _, name = path.rpartition("/")
        parent_id = mapping.get(parent_path) if parent_path else None
        try:
            directory = await _owui_request(
                "POST",
                f"/knowledge/{kb_id}/dirs/create",
                json={"name": name, "parent_id": parent_id},
            )
        except OWUIError as e:
            logger.error("Could not create directory", path=path, error=str(e))
            continue
        directory_id = directory.get("id") if isinstance(directory, dict) else None
        if directory_id:
            mapping[path] = directory_id
            created.append(path)
        else:
            logger.error("Directory create returned no id", path=path)

    return {"map": mapping, "created": created}


async def _upload_entry(kb_id: str, entry: dict, directory_map: dict) -> dict:
    """Upload one manifest entry and return the resulting file id."""
    directory_id = directory_map.get(entry["path"])
    payload = await _owui_upload_file(
        kb_id, directory_id, entry["filename"], entry["content"]
    )
    file_id = payload.get("id") if isinstance(payload, dict) else None
    if not file_id:
        raise OWUIError(f"Upload of {entry['filename']!r} returned no file id")
    return {"file_id": file_id}


async def _remove_kb_file(kb_id: str, file_id: str) -> None:
    """Detach a file from the knowledge base (and delete it if retained)."""
    await _owui_request(
        "POST", f"/knowledge/{kb_id}/file/remove", json={"file_id": file_id}
    )


async def _managed_file_ids(kb_id: str, directory_map: dict) -> set:
    """File ids living inside the managed notes directory."""
    managed: set = set()
    for path, directory_id in (directory_map or {}).items():
        if not _in_notes_dir(path):
            continue
        try:
            listing = await _owui_request(
                "GET",
                f"/knowledge/{kb_id}/files",
                params={"directory_id": directory_id, "limit": 200},
            )
        except OWUIError as e:
            logger.warning("Could not list directory contents", path=path, error=str(e))
            continue
        for item in listing.get("items") or []:
            file_id = item.get("id")
            if file_id:
                managed.add(file_id)
    return managed


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------


async def _sync_to_kb(kb_id: Optional[str] = None) -> dict:
    """Perform an incremental sync of notes to a Knowledge Base."""
    kb_id = kb_id or settings.NOTES_SYNC_KB_ID

    if not kb_id:
        raise HTTPException(
            status_code=400,
            detail="No Knowledge Base ID specified. Set NOTES_SYNC_KB_ID or pass kb_id as a parameter.",
        )

    # Fail with a config error up front. Otherwise a missing token surfaces as
    # an opaque "could not reach any endpoint" several steps later.
    settings.require_owui_config()

    _ensure_sync_state()
    logger.info("Starting notes sync", kb_id=kb_id)

    notes = await _fetch_notes()
    manifest = await _build_manifest(notes)
    by_key = {(entry["path"], entry["filename"]): entry for entry in manifest}

    results = {
        "total_notes": len(notes),
        "total_files": len(manifest),
        "needs_upload": 0,
        "uploaded": [],
        "failed": [],
        "skipped": [],
        "removed": [],
        "cleaned_up": [],
        "created_dirs": [],
        "pending_upload": 0,
    }

    # The diff is authoritative: the persisted manifest can be stale (a note
    # edited while the service was down) and the KB can be changed from the UI.
    try:
        diff = await _owui_request(
            "POST",
            f"/knowledge/{kb_id}/sync/diff",
            json={"manifest": _wire_manifest(manifest)},
        )
    except OWUIError as e:
        logger.error("Sync diff failed", error=str(e))
        results["error"] = str(e)
        sync_status.last_error = str(e)
        sync_status.last_sync = datetime.now(timezone.utc).isoformat()
        return results

    added = diff.get("added") or []
    modified = diff.get("modified") or []
    deleted = diff.get("deleted") or []
    unmodified_count = int(diff.get("unmodified_count") or 0)

    results["plan"] = {
        "added": len(added),
        "modified": len(modified),
        "deleted": len(deleted),
        "mkdir": diff.get("mkdir") or [],
        "unmodified": unmodified_count,
    }

    directory_map = dict(diff.get("directory_map") or {})

    mkdir = [p for p in (diff.get("mkdir") or []) if p]
    if mkdir:
        ensured = await _ensure_directories(kb_id, mkdir, directory_map)
        directory_map = ensured["map"]
        results["created_dirs"] = ensured["created"]

    # ── Pair the plan with local content ──
    # (manifest entry, stale file id to drop after the replacement lands)
    targets: list = []
    for entry, stale_file_id in [(e, None) for e in added] + [
        (e, e.get("stale_file_id")) for e in modified
    ]:
        item = by_key.get((entry.get("path") or "", entry.get("filename")))
        if not item:
            results["skipped"].append(
                {
                    "filename": entry.get("filename"),
                    "reason": "not found in local manifest",
                }
            )
            continue
        targets.append((item, stale_file_id))

    results["needs_upload"] = len(targets)

    # ── Upload ──
    # Sequential on purpose: each upload embeds synchronously, and a burst of
    # parallel embedding requests is what saturates the CPU on this host.
    succeeded: set = set()
    failed_keys: set = set()
    for item, stale_file_id in targets:
        key = (item["path"], item["filename"])
        try:
            outcome = await _upload_entry(kb_id, item, directory_map)
        except OWUIError as e:
            logger.error("Upload failed", filename=item["filename"], error=str(e))
            failed_keys.add(key)
            results["failed"].append({"filename": item["filename"], "error": str(e)})
            continue

        succeeded.add(key)
        results["uploaded"].append(
            {
                "filename": item["filename"],
                "path": item["path"],
                "note_id": item["note_id"],
                "page_id": item["page_id"],
                "file_id": outcome["file_id"],
            }
        )

        # Replace only after the new copy is safely in the KB, so a failure
        # here leaves the previous note in place rather than nothing at all.
        if stale_file_id:
            try:
                await _remove_kb_file(kb_id, stale_file_id)
                results["removed"].append(stale_file_id)
            except OWUIError as e:
                logger.warning(
                    "Could not remove stale file",
                    file_id=stale_file_id,
                    filename=item["filename"],
                    error=str(e),
                )
                results.setdefault("warnings", []).append(
                    {
                        "operation": "remove_stale",
                        "file_id": stale_file_id,
                        "error": str(e),
                    }
                )

    results["pending_upload"] = len(results["failed"])

    # ── Prune notes that no longer exist ──
    if deleted and settings.NOTES_SYNC_PRUNE:
        managed = await _managed_file_ids(kb_id, directory_map)
        file_ids = [d.get("file_id") for d in deleted if d.get("file_id") in managed]
        protected = [
            {"file_id": d.get("file_id"), "filename": d.get("filename")}
            for d in deleted
            if d.get("file_id") not in managed
        ]
        if protected:
            # Not ours: the diff covers the whole KB, so leave these alone.
            results["skipped_prune"] = protected

        orphan_dirs = [
            directory_map[path]
            for path in directory_map
            if _in_notes_dir(path)
            and directory_map[path] in (diff.get("rmdir") or [])
        ]

        if file_ids or orphan_dirs:
            try:
                await _owui_request(
                    "POST",
                    f"/knowledge/{kb_id}/sync/cleanup",
                    json={"file_ids": file_ids, "dir_ids": orphan_dirs},
                )
                results["cleaned_up"] = file_ids
                results["cleaned_dirs"] = orphan_dirs
                logger.info(
                    "Pruned stale notes",
                    files=len(file_ids),
                    dirs=len(orphan_dirs),
                )
            except OWUIError as e:
                logger.error("Cleanup failed", error=str(e))
                results.setdefault("warnings", []).append(
                    {"operation": "cleanup", "error": str(e)}
                )

    # ── Bookkeeping: record only what is actually in the KB ──
    # Anything that failed, or that we asked to upload and did not land, is left
    # out of the state so the next run retries it. Recording optimistically is
    # what made a failed upload look synced and never get retried.
    new_state: dict = {}
    for key, entry in by_key.items():
        if key in failed_keys:
            continue
        if key in targets and key not in succeeded:
            continue
        mapping_key = _entry_key(entry)
        new_state[mapping_key] = entry["checksum"]

    sync_status.files = new_state
    _save_manifest(new_state)

    if results["failed"]:
        sync_status.last_error = (
            f"{len(results['failed'])} of {len(targets)} uploads failed"
        )
    else:
        sync_status.last_error = None
    sync_status.last_sync = datetime.now(timezone.utc).isoformat()
    sync_status.last_count = len(notes)

    logger.info(
        "Notes sync complete",
        kb_id=kb_id,
        total_notes=len(notes),
        uploaded=len(results["uploaded"]),
        failed=len(results["failed"]),
        cleaned=len(results["cleaned_up"]),
    )

    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/", summary="Trigger a notes-to-KB sync", operation_id="NotesSyncTrigger")
async def trigger_sync(kb_id: Optional[str] = None) -> dict:
    """
    Manually trigger a sync of all Open-WebUI notes into a Knowledge Base.

    This performs an incremental sync: only new or modified notes are uploaded.
    """
    try:
        results = await _sync_to_kb(kb_id)
        failed = results.get("failed") or []
        if results.get("error"):
            status = "error"
        elif failed:
            status = "partial" if results.get("uploaded") else "error"
        else:
            status = "success"
        return {
            "status": status,
            "synced_at": sync_status.last_sync,
            **results,
        }
    except HTTPException:
        raise
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001 - surface as a response, not a 500
        logger.error("Sync failed", error=str(e))
        return {"status": "error", "message": str(e)}


@router.get("/status", summary="Get sync status", operation_id="NotesSyncStatus")
async def get_sync_status() -> dict:
    """Get the current status of the notes sync."""
    _ensure_sync_state()
    return {
        "last_sync": sync_status.last_sync,
        "last_count": sync_status.last_count,
        "last_error": sync_status.last_error,
        "tracked_files": len(sync_status.files),
        "kb_id": settings.NOTES_SYNC_KB_ID or "not configured",
        "auto_sync_enabled": settings.NOTES_SYNC_INTERVAL > 0,
        "sync_interval_seconds": settings.NOTES_SYNC_INTERVAL,
        "prune_enabled": settings.NOTES_SYNC_PRUNE,
    }


@router.get("/health", summary="Check OWUI connection", operation_id="NotesSyncHealth")
async def owui_health() -> dict:
    """Check if Open-WebUI is configured and accessible."""
    if not settings.is_owui_configured():
        return {"status": "error", "message": "Open-WebUI is not configured"}

    try:
        await _owui_request("GET", "/notes/", params={"count": 1})
    except OWUIError as e:
        return {"status": "error", "message": str(e)}

    payload = {
        "status": "ok",
        "instance": settings.OWUI_INSTANCE_URL,
        "notes_accessible": True,
    }

    # A wrong NOTES_SYNC_KB_ID is otherwise invisible until the first sync, so
    # report it here where the deploy verify step can assert on it.
    kb_id = settings.NOTES_SYNC_KB_ID
    if kb_id:
        try:
            knowledge = await _owui_request("GET", f"/knowledge/{kb_id}")
            payload["kb_accessible"] = bool(knowledge)
            payload["kb_name"] = (knowledge or {}).get("name")
        except OWUIError as e:
            payload["kb_accessible"] = False
            payload["kb_error"] = str(e)
    else:
        payload["kb_accessible"] = False
        payload["kb_error"] = "NOTES_SYNC_KB_ID is not set"

    return payload


@router.get(
    "/debug", summary="Debug notes API endpoints", operation_id="NotesSyncDebug"
)
async def debug_endpoints() -> dict:
    """
    Test all known notes API endpoints and return their raw responses.

    Use this to diagnose why notes aren't being fetched. Shows status codes,
    response types, and first 500 chars of each response.
    """
    if not settings.is_owui_configured():
        return {"status": "error", "message": "Open-WebUI is not configured"}

    endpoints = [
        "/api/v1/notes",
        "/api/notes",
        "/api/v1/workspace/notes",
        "/notes",
        "/api/v1/notes/search",
    ]

    results = []
    for path in endpoints:
        try:
            url = f"{settings.owui_api_url}{path}"
            headers = settings.owui_headers
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request("GET", url, headers=headers)
                results.append(
                    {
                        "endpoint": path,
                        "status_code": response.status_code,
                        "content_type": response.headers.get("content-type", "unknown"),
                        "body_preview": response.text[:500],
                        "body_length": len(response.text),
                    }
                )
        except Exception as e:  # noqa: BLE001 - debug endpoint reports everything
            results.append(
                {
                    "endpoint": path,
                    "error": str(e),
                }
            )

    return {
        "owui_url": settings.OWUI_INSTANCE_URL,
        "endpoints_tested": len(results),
        "results": results,
    }
