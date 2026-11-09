"""Regression tests for the notes -> knowledge-base upload path.

The bug class this covers: the sync computed a correct diff plan and uploaded
nothing. Everything below the plan was unimplemented, so the interesting
failures are all "reported success, wrote nothing" or "wrote the wrong bytes".

Covered:

* a sync actually puts every note in the KB, at the managed path
* note content is the FULL note, not the 1000-char list preview
* a second sync is a no-op (no duplicate uploads)
* a modified note replaces the old KB file instead of duplicating it
* a failed upload is reported as failed and is retried next run
* files that are not ours are never deleted
* filename collision handling, multi-page notes, empty notes

NOTE on configuration: do NOT rely on module-level ``os.environ`` here.
``app.config.Settings`` reads the environment when the *class body* executes,
i.e. at import time, and ``tests.test_gitea_*`` import ``app.config`` first. Any
``os.environ.setdefault`` in this module therefore runs too late to affect
``settings.OWUI_TOKEN``, and the suite only passed locally because the dev
container happens to have a real token in its env. The configuration is forced
onto the shared ``settings`` object in ``Base.setUp`` instead.
"""

import asyncio
import hashlib
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx

from app import notes_sync
from app.notes_sync import (
    _MAX_NAME,
    _MAX_STEM,
    _build_manifest,
    _note_pages,
    _safe_filename,
    _sync_to_kb,
)
from tests.fake_owui import TRUNCATE_AT, FakeOWUI

KB = "kb-1"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Base(unittest.TestCase):
    #: Settings attributes this test needs; restored in tearDown so the order
    #: tests run in cannot leak configuration into the Gitea suites.
    _FORCED = {
        "OWUI_TOKEN": "test-owui-token",
        "OWUI_INSTANCE_URL": "https://owui.example",
        "GITEA_TOKEN": "test-token",
        "GITEA_INSTANCE_URL": "https://gitea.example",
        "NOTES_SYNC_KB_ID": KB,
        "NOTES_SYNC_PRUNE": True,
    }

    def setUp(self):
        self.fake = FakeOWUI(kb_id=KB)
        self._orig_client = httpx.AsyncClient

        self._saved = {}
        for name, value in self._FORCED.items():
            self._saved[name] = getattr(notes_sync.settings, name)
            setattr(notes_sync.settings, name, value)

        self._saved["EVIDENCE_DIR"] = notes_sync.settings.EVIDENCE_DIR
        self._tmp = tempfile.mkdtemp(prefix="owc-notes-test-")
        notes_sync.settings.EVIDENCE_DIR = self._tmp

        self._saved_status = (
            notes_sync.sync_status.files,
            notes_sync.sync_status._loaded,
            notes_sync.sync_status.last_error,
        )
        notes_sync.sync_status.files = {}
        notes_sync.sync_status._loaded = True
        notes_sync.sync_status.last_error = None

    def tearDown(self):
        httpx.AsyncClient = self._orig_client
        for name, value in self._saved.items():
            setattr(notes_sync.settings, name, value)
        (
            notes_sync.sync_status.files,
            notes_sync.sync_status._loaded,
            notes_sync.sync_status.last_error,
        ) = self._saved_status

    def run_sync(self):
        """Run the sync against the fake and return its results."""
        self.fake.install(httpx)
        return asyncio.run(_sync_to_kb())


class TestConfigurationIsolation(Base):
    """The suite must not depend on a real token being in the environment."""

    def test_owui_is_configured_for_the_test(self):
        self.assertTrue(notes_sync.settings.is_owui_configured())

    def test_a_missing_token_fails_loudly(self):
        """Proves the assertion above is doing work, not passing by accident."""
        notes_sync.settings.OWUI_TOKEN = ""
        with self.assertRaises(Exception) as ctx:
            self.run_sync()
        self.assertIn("not configured", str(ctx.exception))


class TestFullSyncWritesEverything(Base):
    def test_every_note_lands_in_the_kb(self):
        self.fake.add_note("n1", "Alpha", "# Alpha\n\nfirst")
        self.fake.add_note("n2", "Beta", "# Beta\n\nsecond")

        results = self.run_sync()

        self.assertEqual(len(results["uploaded"]), 2)
        self.assertEqual(results["failed"], [])
        self.assertEqual(
            self.fake.files_in_kb(),
            [("Alpha.md", "notes"), ("Beta.md", "notes")],
        )

    def test_notes_directory_is_created(self):
        self.fake.add_note("n1", "Alpha", "body")
        results = self.run_sync()
        self.assertEqual(results["created_dirs"], ["notes"])
        self.assertEqual(len(self.fake.directories), 1)

    def test_upload_uses_multipart_and_links_to_the_kb(self):
        self.fake.add_note("n1", "Alpha", "body")
        self.run_sync()

        posts = self.fake.calls_matching("POST", "/files")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["files"], ["file"])
        # A JSON content-type on a multipart post means httpx never added a
        # boundary; the fake rejects that, so reaching here proves it is right.
        # Background processing returns an unlinked file the diff cannot see,
        # which would make every sync re-upload. This must stay false.
        self.assertEqual(posts[0][2]["params"]["process_in_background"], "false")


class TestFullContentNotThePreview(Base):
    def test_long_note_is_uploaded_in_full(self):
        body = "x" * 5000
        self.fake.add_note("n1", "Long", body)

        results = self.run_sync()

        self.assertEqual(len(results["uploaded"]), 1)
        stored = next(iter(self.fake.files.values()))
        self.assertEqual(stored["data"]["file_hash"], sha(body))

    def test_short_note_uses_full_body_not_the_preview(self):
        """A note under the preview limit would pass even with the bug."""
        body = "short body"
        self.fake.add_note("n1", "Short", body)
        self.run_sync()
        stored = next(iter(self.fake.files.values()))
        self.assertEqual(stored["data"]["file_hash"], sha(body))

    def test_manifest_fetches_each_note_by_id(self):
        """The list endpoint truncates; the manifest must not."""
        self.fake.add_note("n1", "Long", "y" * 3000)
        self.run_sync()

        detail_calls = [
            c
            for c in self.fake.calls_matching("GET", "/notes/")
            if c[1].endswith("/notes/n1")
        ]
        self.assertEqual(len(detail_calls), 1, "must fetch the full note by id")


class TestIdempotency(Base):
    def test_second_sync_uploads_nothing(self):
        self.fake.add_note("n1", "Alpha", "body")
        self.fake.add_note("n2", "Beta", "other")

        first = self.run_sync()
        self.assertEqual(len(first["uploaded"]), 2)

        second = self.run_sync()
        self.assertEqual(second["uploaded"], [], "second sync re-uploaded unchanged notes")
        self.assertEqual(second["plan"]["unmodified"], 2)
        self.assertEqual(len(self.fake.files), 2, "duplicate KB files were created")

    def test_second_sync_posts_no_files(self):
        self.fake.add_note("n1", "Alpha", "body")
        self.run_sync()
        before = len(self.fake.calls_matching("POST", "/files"))
        self.run_sync()
        after = len(self.fake.calls_matching("POST", "/files"))
        self.assertEqual(before, after, "a no-op sync still uploaded")

    def test_state_survives_a_restart(self):
        """A fresh process must not re-upload everything."""
        self.fake.add_note("n1", "Alpha", "body")
        self.run_sync()
        self.assertEqual(len(self.fake.files), 1)

        # Simulate a restart: in-memory state gone, persisted state remains.
        notes_sync.sync_status.files = {}
        notes_sync.sync_status._loaded = False

        results = self.run_sync()
        self.assertEqual(results["uploaded"], [])
        self.assertEqual(len(self.fake.files), 1)


class TestModifyReplaces(Base):
    def _edit(self, note_id, text):
        self.fake.notes[note_id]["data"]["content"]["md"] = text
        self.fake.notes[note_id]["data"]["pages"][0]["content"]["md"] = text

    def test_changed_note_replaces_the_old_kb_file(self):
        self.fake.add_note("n1", "Alpha", "version one")
        self.run_sync()
        old_id = next(iter(self.fake.files))
        self.assertEqual(len(self.fake.files), 1)

        self._edit("n1", "version two")
        results = self.run_sync()

        self.assertEqual(results["plan"]["modified"], 1)
        self.assertEqual(len(results["uploaded"]), 1)
        self.assertIn(old_id, results["removed"])
        self.assertEqual(len(self.fake.files), 1, "the stale file was left behind")

        stored = next(iter(self.fake.files.values()))
        self.assertEqual(stored["data"]["file_hash"], sha("version two"))

    def test_stale_file_is_kept_when_the_replacement_fails(self):
        """Losing the old copy is worse than a duplicated note."""
        self.fake.add_note("n1", "Alpha", "version one")
        self.run_sync()
        old_id = next(iter(self.fake.files))

        self._edit("n1", "version two")
        self.fake.fail_upload_of.add("Alpha.md")

        results = self.run_sync()

        self.assertEqual(results["removed"], [])
        self.assertIn(old_id, self.fake.files, "the previous version was deleted")


class TestFailuresAreReported(Base):
    def _edit(self, note_id, text):
        self.fake.notes[note_id]["data"]["content"]["md"] = text
        self.fake.notes[note_id]["data"]["pages"][0]["content"]["md"] = text

    def test_failed_upload_is_not_recorded_as_synced(self):
        self.fake.add_note("n1", "Broken", "body")
        self.fake.fail_upload_of.add("Broken.md")

        results = self.run_sync()

        self.assertEqual(results["uploaded"], [])
        self.assertEqual(len(results["failed"]), 1)
        self.assertEqual(results["pending_upload"], 1)
        self.assertEqual(self.fake.files_in_kb(), [])
        # The whole point: a failure must not be marked synced.
        self.assertEqual(notes_sync.sync_status.files, {})
        self.assertIn("failed", notes_sync.sync_status.last_error or "")

    def test_failed_modification_is_not_recorded_as_synced(self):
        self.fake.add_note("n1", "Alpha", "version one")
        self.run_sync()

        self._edit("n1", "version two")
        self.fake.fail_upload_of.add("Alpha.md")
        self.run_sync()

        self.assertEqual(
            notes_sync.sync_status.files,
            {},
            "a failed modification was recorded as synced and will never retry",
        )

    def test_failed_upload_is_retried_on_the_next_run(self):
        self.fake.add_note("n1", "Flaky", "body")
        self.fake.fail_upload_of.add("Flaky.md")
        self.run_sync()

        self.fake.fail_upload_of.clear()
        results = self.run_sync()

        self.assertEqual(len(results["uploaded"]), 1)
        self.assertEqual(results["failed"], [])
        self.assertEqual(self.fake.files_in_kb(), [("Flaky.md", "notes")])

    def test_diff_failure_surfaces_and_writes_nothing(self):
        self.fake.add_note("n1", "Alpha", "body")
        self.fake.fail_diff = True

        results = self.run_sync()

        self.assertIn("error", results)
        self.assertEqual(self.fake.files_in_kb(), [])

    def test_one_failure_does_not_block_the_others(self):
        self.fake.add_note("n1", "Broken", "body")
        self.fake.add_note("n2", "Fine", "body")
        self.fake.fail_upload_of.add("Broken.md")

        results = self.run_sync()

        self.assertEqual(len(results["uploaded"]), 1)
        self.assertEqual(len(results["failed"]), 1)
        self.assertEqual(self.fake.files_in_kb(), [("Fine.md", "notes")])


class TestDeletionScope(Base):
    """The diff's `deleted` list covers the whole KB, not just our directory."""

    def _add_foreign_file(self):
        # A document someone added to the KB by hand, outside `notes/`.
        self.fake.files["foreign-1"] = {
            "filename": "Handbook.pdf",
            "directory_id": None,
            "linked": True,
            "data": {"file_hash": "deadbeef"},
        }

    def test_foreign_file_is_never_deleted(self):
        self.fake.add_note("n1", "Alpha", "body")
        self.run_sync()

        self._add_foreign_file()
        del self.fake.notes["n1"]
        results = self.run_sync()

        self.assertIn("foreign-1", self.fake.files, "deleted a file we do not own")
        self.assertEqual(results["plan"]["deleted"], 2)
        pruned = {s["file_id"] for s in results.get("skipped_prune") or []}
        self.assertIn("foreign-1", pruned)

    def test_a_foreign_only_kb_is_left_completely_untouched(self):
        """No notes at all: the sync must not treat the KB as garbage."""
        self._add_foreign_file()
        results = self.run_sync()

        self.assertEqual(results["uploaded"], [])
        self.assertEqual(results["cleaned_up"], [])
        self.assertEqual(results["plan"]["deleted"], 1)
        self.assertIn("foreign-1", self.fake.files)

    def test_our_own_deleted_note_is_pruned(self):
        self.fake.add_note("n1", "Alpha", "body")
        self.fake.add_note("n2", "Beta", "other")
        self.run_sync()
        self.assertEqual(len(self.fake.files), 2)

        del self.fake.notes["n1"]
        results = self.run_sync()

        self.assertEqual(len(results["cleaned_up"]), 1)
        self.assertEqual(self.fake.files_in_kb(), [("Beta.md", "notes")])

    def test_prune_can_be_disabled(self):
        self.fake.add_note("n1", "Alpha", "body")
        self.run_sync()
        del self.fake.notes["n1"]

        notes_sync.settings.NOTES_SYNC_PRUNE = False
        results = self.run_sync()

        self.assertEqual(results["cleaned_up"], [])
        self.assertEqual(len(self.fake.files), 1)


class TestManifestShape(unittest.TestCase):
    def test_wire_shape_has_no_extra_keys(self):
        manifest = [
            {
                "filename": "a.md",
                "base": "a",
                "path": "notes",
                "checksum": "c",
                "size": 1,
                "content": "x",
                "note_id": "n1",
                "page_id": "main",
            }
        ]
        wire = notes_sync._wire_manifest(manifest)
        self.assertEqual(set(wire[0]), {"filename", "path", "checksum", "size"})

    DUPES = [
        {"id": "aaaaaaaa-1", "title": "Same", "data": {"content": {"md": "one"}}},
        {"id": "bbbbbbbb-2", "title": "Same", "data": {"content": {"md": "two"}}},
    ]

    def test_duplicate_titles_do_not_collide(self):
        manifest = asyncio.run(_build_manifest(self.DUPES))

        names = [m["filename"] for m in manifest]
        self.assertEqual(len(set(names)), 2, f"colliding filenames: {names}")
        self.assertTrue(all(n.endswith(".md") for n in names))

        # Deterministic: the same input must produce the same names, otherwise
        # every sync sees a rename and re-uploads.
        again = [m["filename"] for m in asyncio.run(_build_manifest(self.DUPES))]
        self.assertEqual(names, again)

    def test_collision_suffix_is_not_doubled(self):
        manifest = asyncio.run(_build_manifest(self.DUPES))
        for entry in manifest:
            self.assertEqual(entry["filename"], f"{entry['base']}.md")
            self.assertNotIn("--", entry["filename"])

    def test_multi_page_note_produces_one_file_per_page(self):
        note = {
            "id": "n1",
            "title": "Project",
            "data": {
                "pages": [
                    {"id": "main", "title": "Project", "content": {"md": "overview"}},
                    {"id": "p2", "title": "Details", "content": {"md": "depth"}},
                ]
            },
        }
        manifest = asyncio.run(_build_manifest([note]))
        self.assertEqual(len(manifest), 2)
        self.assertEqual(manifest[0]["page_id"], "main", "main page must come first")
        self.assertNotEqual(manifest[0]["filename"], manifest[1]["filename"])
        self.assertIn("Details", manifest[1]["filename"])

    def test_names_never_exceed_the_limit(self):
        note = {"id": "n1", "title": "z" * 400, "data": {"content": {"md": "body"}}}
        manifest = asyncio.run(_build_manifest([note]))
        for entry in manifest:
            self.assertLessEqual(len(entry["filename"]), _MAX_NAME)

    def test_empty_note_produces_no_entry(self):
        """OWUI 400s on empty content, so it must be skipped, not uploaded."""
        notes = [{"id": "n1", "title": "Empty", "data": {"content": {"md": "   "}}}]
        self.assertEqual(asyncio.run(_build_manifest(notes)), [])

    def test_legacy_content_shape_still_works(self):
        notes = [{"id": "n1", "title": "Legacy", "data": {"content": {"md": "text"}}}]
        manifest = asyncio.run(_build_manifest(notes))
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["content"], "text")

    def test_note_pages_falls_back_to_legacy_content(self):
        pages = _note_pages({"title": "T", "data": {"content": {"md": "legacy"}}})
        self.assertEqual(pages, [("main", "T", "legacy")])


class TestFilenames(unittest.TestCase):
    def test_unsafe_characters_are_stripped(self):
        self.assertEqual(_safe_filename('a/b\\c:d*e?f"g<h>i|j'), "a b c d e f g h i j")

    def test_stem_is_capped_so_the_extension_still_fits(self):
        stem = _safe_filename("z" * 500)
        self.assertEqual(len(stem), _MAX_STEM)
        self.assertLessEqual(len(stem) + len(".md"), _MAX_NAME)

    def test_empty_title_gets_a_fallback(self):
        self.assertEqual(_safe_filename(""), "untitled")

    def test_trailing_dot_and_whitespace_removed(self):
        self.assertEqual(_safe_filename("  name.  "), "name")


class TestPreviewAssumption(unittest.TestCase):
    """Guards the premise of the truncation fix against the fake itself."""

    def test_fake_truncates_the_list_but_not_the_detail(self):
        fake = FakeOWUI()
        body = "z" * (TRUNCATE_AT + 500)
        fake.add_note("n1", "T", body)

        listing = fake._handle("GET", "http://owui/api/v1/notes/", {})
        self.assertEqual(len(listing.json()[0]["data"]["content"]["md"]), TRUNCATE_AT)

        detail = fake._handle("GET", "http://owui/api/v1/notes/n1", {})
        self.assertEqual(len(detail.json()["data"]["content"]["md"]), len(body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
