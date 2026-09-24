"""Tests for the FileMetaSync bundled plugin entrypoint."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Dynamically import entrypoint module
_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "entrypoint.py"
_SPEC = importlib.util.spec_from_file_location("file_meta_sync_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["file_meta_sync_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class TestDiscoverCandidates:
    """Tests for candidate discovery."""

    def test_discover_candidates_registers_new_files(self) -> None:
        """Discover new cover candidate files and emit add-writes with media types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            book_dir = lib_root / "books"
            book_dir.mkdir()

            # Create files: book.cover1.png, book-alt.jpg, other.png
            (book_dir / "book.cover1.png").write_bytes(b"png_data")
            (book_dir / "book-alt.jpg").write_bytes(b"jpg_data")
            (book_dir / "other.png").write_bytes(b"png_data")

            # Existing assets (empty)
            assets = []

            # Discover candidates for stem "book"
            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            # Should find .cover1.png and -alt.jpg, but not other.png
            assert len(writes) == 2
            filenames = {w["name"] for w in writes}
            assert "book.cover1.png" in filenames
            assert "book-alt.jpg" in filenames
            assert "other.png" not in filenames

            # Check media types
            png_write = next(w for w in writes if w["name"] == "book.cover1.png")
            jpg_write = next(w for w in writes if w["name"] == "book-alt.jpg")
            assert png_write["media_type"] == "image/png"
            assert jpg_write["media_type"] == "image/jpeg"

            # Check structure
            assert all("kind" in w and w["kind"] == "cover_candidate" for w in writes)
            assert all("data" in w and "path" not in w for w in writes)

    def test_discover_candidates_deregisters_missing(self) -> None:
        """Emit delete-write for assets with missing files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            book_dir = lib_root / "books"
            book_dir.mkdir()

            # Existing asset with no file (own row: the core refuses writes to another's)
            assets = [
                {
                    "kind": "cover_candidate",
                    "name": "book.cover1.png",
                    "path": "books/book.cover1.png",
                    "namespace": "file_meta_sync",
                }
            ]

            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            # Should emit delete for missing asset
            assert len(writes) == 1
            assert writes[0]["name"] == "book.cover1.png"
            assert writes[0].get("delete") is True

    def test_discover_candidates_ignores_non_image_extensions(self) -> None:
        """Only consider image extensions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)
            # Create various files
            (book_dir / "book.cover.txt").write_text("text")
            (book_dir / "book.cover.pdf").write_bytes(b"pdf")
            (book_dir / "book.cover1.png").write_bytes(b"png")

            assets = []
            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            # Only PNG should be found
            assert len(writes) == 1
            assert writes[0]["name"] == "book.cover1.png"

    def test_discover_imports_bytes_with_source_and_sha(self) -> None:
        """A new sidecar image is imported as a stored write with source_file/sha256 meta."""
        import base64
        import hashlib

        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)
            (book_dir / "book.cover1.png").write_bytes(b"img1")

            writes = _MODULE.discover_candidates(book_dir, "book", [])

            assert len(writes) == 1
            w = writes[0]
            assert base64.b64decode(w["data"]) == b"img1"
            assert w["meta"] == {
                "source_file": "book.cover1.png",
                "sha256": hashlib.sha256(b"img1").hexdigest(),
            }
            assert w["media_type"] == "image/png"
            assert "path" not in w

    def test_discover_skips_an_unchanged_import(self) -> None:
        """An unchanged sidecar (same sha256 as the stored row) produces no write."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)
            (book_dir / "book.cover1.png").write_bytes(b"img1")

            assets = [
                {
                    "kind": "cover_candidate",
                    "name": "book.cover1.png",
                    "namespace": "file_meta_sync",
                    "storage": "store",
                    "path": "/data/assets/x.png",
                    "meta": {
                        "source_file": "book.cover1.png",
                        "sha256": hashlib.sha256(b"img1").hexdigest(),
                    },
                }
            ]

            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            assert writes == []

    def test_discover_reimports_a_changed_sidecar(self) -> None:
        """A changed sidecar (different sha256) is re-imported with the new hash."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)
            (book_dir / "book.cover1.png").write_bytes(b"img2")

            assets = [
                {
                    "kind": "cover_candidate",
                    "name": "book.cover1.png",
                    "namespace": "file_meta_sync",
                    "storage": "store",
                    "path": "/data/assets/x.png",
                    "meta": {
                        "source_file": "book.cover1.png",
                        "sha256": hashlib.sha256(b"img1").hexdigest(),
                    },
                }
            ]

            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            assert len(writes) == 1
            assert writes[0]["meta"]["sha256"] == hashlib.sha256(b"img2").hexdigest()

    def test_discover_deletes_own_stored_row_whose_source_vanished(self) -> None:
        """A stored row whose source sidecar no longer exists on disk is deleted."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)

            assets = [
                {
                    "kind": "cover_candidate",
                    "name": "book.cover1.png",
                    "namespace": "file_meta_sync",
                    "storage": "store",
                    "path": "/data/assets/x.png",
                    "meta": {
                        "source_file": "book.cover1.png",
                        "sha256": hashlib.sha256(b"img1").hexdigest(),
                    },
                }
            ]

            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            assert writes == [
                {"kind": "cover_candidate", "name": "book.cover1.png", "delete": True}
            ]

    def test_discover_never_touches_another_plugins_candidate(self) -> None:
        """A cover_candidate row owned by another plugin's namespace is never written or deleted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)

            assets = [
                {
                    "kind": "cover_candidate",
                    "name": "flux-dev",
                    "namespace": "llm_cover",
                    "storage": "store",
                    "path": "/x",
                    "meta": {"model": "flux-dev"},
                }
            ]

            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            assert writes == []

    def test_discover_converts_own_library_row_to_store(self) -> None:
        """An own library row whose file still exists is converted to a stored write."""
        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)
            (book_dir / "book.cover1.png").write_bytes(b"img1")

            assets = [
                {
                    "kind": "cover_candidate",
                    "name": "book.cover1.png",
                    "namespace": "file_meta_sync",
                    "storage": "library",
                    "path": "books/book.cover1.png",
                }
            ]

            writes = _MODULE.discover_candidates(book_dir, "book", assets)

            assert len(writes) == 1
            assert "data" in writes[0]
            assert writes[0]["name"] == "book.cover1.png"
            assert writes[0].get("delete") is None

    def test_discover_ignores_the_cover_export(self) -> None:
        """The plugin's own <stem>.cover.png export is never treated as a candidate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            book_dir = Path(tmpdir)
            (book_dir / "book.cover.png").write_bytes(b"cover_export")

            writes = _MODULE.discover_candidates(book_dir, "book", [])

            assert writes == []

    def test_discover_skips_oversized_files(self, monkeypatch) -> None:
        """A sidecar over the size limit produces no write; process_book logs the skip."""
        monkeypatch.setattr(_MODULE, "_MAX_IMPORT_BYTES", 3)
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            books_dir = lib_root / "books"
            books_dir.mkdir()
            (books_dir / "book.c.png").write_bytes(b"toolong")

            writes = _MODULE.discover_candidates(books_dir, "book", [])
            assert writes == []

            book = {
                "book_id": "b1",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }
            result = _MODULE.process_book(book, lib_root)

            assert result is not None
            warnings = [log for log in result["logs"] if log["level"] == "warning"]
            assert any(
                "1 candidate file(s) over 25 MiB not imported" in log["message"] for log in warnings
            )


class TestMergeText:
    """Tests for 3-way text merge (synopsis, T2I)."""

    def test_synopsis_file_newer_wins(self) -> None:
        """Unattended tracked conflict: both sides changed → app wins."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file synopsis")

            db_value = "db synopsis"
            synced_hash = "old_hash"  # Neither matches current
            logs: list[dict] = []

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id", db_value, file_path, synced_hash, logs
            )

            # Both changed, unattended → app wins, file is rewritten
            assert resolved_value is None
            assert file_path.read_text() == "db synopsis"
            assert resolved_hash is not None
            assert resolved_mtime is not None
            assert len(logs) == 1
            assert logs[0]["level"] == "warning"

    def test_synopsis_db_newer_writes_file(self) -> None:
        """DB newer than file writes to file, returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("old file")

            db_value = "new db synopsis"
            # Assume file hash matches synced_hash (file unchanged)
            file_text = "old file"
            import hashlib

            file_hash = hashlib.sha256(file_text.encode()).hexdigest()
            synced_hash = file_hash

            logs: list[dict] = []

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id", db_value, file_path, synced_hash, logs
            )

            # Only DB changed → file written from DB
            assert resolved_value is None
            assert resolved_hash is not None
            assert resolved_mtime is not None
            # File should be updated
            assert file_path.read_text() == "new db synopsis"

    def test_synopsis_unchanged_is_nop(self) -> None:
        """Both unchanged since last sync returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_text = "unchanged"
            file_path.write_text(file_text)

            import hashlib

            file_hash = hashlib.sha256(file_text.encode()).hexdigest()

            db_value = "unchanged"
            synced_hash = file_hash  # Both unchanged

            logs: list[dict] = []

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id", db_value, file_path, synced_hash, logs
            )

            assert resolved_value is None
            assert resolved_hash is None
            assert resolved_mtime is None
            assert len(logs) == 0


class TestMergeBytes:
    """Tests for 3-way bytes merge (cover)."""

    def test_cover_file_newer_wins(self) -> None:
        """File newer than DB wins, returns file path and hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.cover.png"
            file_path.write_bytes(b"file_cover_data")

            db_path = None  # Assume DB has no cover
            synced_hash = None
            logs: list[dict] = []

            resolved_path, resolved_hash, resolved_mtime = _MODULE.merge_bytes(
                "book_id", db_path, file_path, synced_hash, logs
            )

            # File exists, DB empty → file value
            assert resolved_path == str(file_path)
            assert resolved_hash is not None
            assert resolved_mtime is not None

    def test_cover_db_newer_writes_file(self) -> None:
        """DB newer than file writes cover from DB."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.cover.png"
            file_path.write_bytes(b"old_cover")

            db_file = Path(tmpdir) / "db_cover.png"
            db_file.write_bytes(b"new_db_cover")

            import hashlib

            file_hash = hashlib.sha256(b"old_cover").hexdigest()
            synced_hash = file_hash  # File unchanged
            logs: list[dict] = []

            resolved_path, resolved_hash, resolved_mtime = _MODULE.merge_bytes(
                "book_id", str(db_file), file_path, synced_hash, logs
            )

            # Only DB changed → file written from DB
            assert resolved_path is None
            assert resolved_hash is not None
            assert resolved_mtime is not None
            assert file_path.read_bytes() == b"new_db_cover"


class TestLegacySynopsisAdoption:
    """Tests for legacy .synopsis.txt adoption."""

    def test_legacy_synopsis_file_adopted(self) -> None:
        """Legacy .synopsis.txt file renamed to .back_cover.txt on first sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()
            library_path = lib_root / dir_part

            # Create only legacy .synopsis.txt
            legacy_file = library_path / "book.synopsis.txt"
            legacy_file.write_text("legacy text")

            # Call process_book with empty synopsis and no synced hash
            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            result = _MODULE.process_book(book, lib_root)

            # Assert new file exists with legacy content
            back_cover_file = library_path / "book.back_cover.txt"
            assert back_cover_file.exists()
            assert back_cover_file.read_text() == "legacy text"

            # Assert legacy file no longer exists
            assert not legacy_file.exists()

            # Assert patch includes adopted synopsis
            assert result is not None
            assert result["fields"]["synopsis"] == "legacy text"
            assert "synopsis.synced_hash" in result["custom_values"]

    def test_both_files_present_back_cover_wins_legacy_untouched(self) -> None:
        """Both files exist: .back_cover.txt wins, legacy file untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()
            library_path = lib_root / dir_part

            # Create both files
            back_cover_file = library_path / "book.back_cover.txt"
            back_cover_file.write_text("new")

            legacy_file = library_path / "book.synopsis.txt"
            legacy_file.write_text("old")

            # Call process_book
            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            result = _MODULE.process_book(book, lib_root)

            # Assert both files still exist
            assert back_cover_file.exists()
            assert legacy_file.exists()

            # Assert .back_cover.txt content wins
            assert result is not None
            assert result["fields"]["synopsis"] == "new"


class TestConflictMostRecentWins:
    """Tests for conflict resolution (ruled precedence)."""

    def test_an_unattended_tracked_conflict_keeps_the_app_version(self) -> None:
        """Both sides changed with synced_hash; unattended default keeps app version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file version")

            db_value = "db version"
            synced_hash = "old_synced_hash"
            logs: list[dict] = []
            notify_calls: list[tuple[str, str]] = []

            def notify_spy(level: str, message: str) -> None:
                notify_calls.append((level, message))

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id",
                db_value,
                file_path,
                synced_hash,
                logs,
                ask=None,
                interactive=False,
                notify=notify_spy,
                book_label="Wuthering Heights",
            )

            # Unattended conflict: app version wins, file is rewritten
            assert resolved_value is None
            assert file_path.read_text() == "db version"
            assert len(logs) == 1
            assert logs[0]["level"] == "warning"
            assert "kept the ebookerr version and rewrote the sidecar" in logs[0]["message"]
            assert 'Conflict on book.back_cover.txt for "Wuthering Heights"' in logs[0]["message"]

            # notify should be called once with correct wording
            assert len(notify_calls) == 1
            assert notify_calls[0][0] == "warning"
            assert "Sidecar conflict" in notify_calls[0][1]
            assert "Wuthering Heights" in notify_calls[0][1]
            assert "ebookerr synopsis" in notify_calls[0][1]

    def test_a_first_sync_adopts_the_sidecar_over_the_db_value(self) -> None:
        """First sync: synced_hash=None, both sides present and differ → adopt file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file version")

            db_value = "db version"
            synced_hash = None
            logs: list[dict] = []
            notify_calls: list[tuple[str, str]] = []

            def notify_spy(level: str, message: str) -> None:
                notify_calls.append((level, message))

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id",
                db_value,
                file_path,
                synced_hash,
                logs,
                ask=None,
                interactive=False,
                notify=notify_spy,
                book_label="Wuthering Heights",
            )

            # First sync: file is adopted, unchanged on disk
            assert resolved_value == "file version"
            assert resolved_hash is not None
            assert resolved_mtime is not None
            assert len(logs) == 1
            assert logs[0]["level"] == "info"
            assert "first sync — adopted the sidecar synopsis" in logs[0]["message"]

            # notify should NOT be called on first sync
            assert len(notify_calls) == 0

    def test_a_first_sync_with_no_db_value_still_adopts(self) -> None:
        """First sync with no DB value: file exists → adopt file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file version")

            db_value = None
            synced_hash = None
            logs: list[dict] = []

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id", db_value, file_path, synced_hash, logs
            )

            # File-only branch (no db value): adopt file
            assert resolved_value == "file version"
            assert resolved_hash is not None
            assert resolved_mtime is not None

    def test_an_attended_conflict_asks_with_the_new_copy(self) -> None:
        """Interactive conflict: ask is called with proper labels, no book_id in message."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file version")

            db_value = "db version"
            synced_hash = "old_synced_hash"
            logs: list[dict] = []
            ask_calls: list[tuple[str, str, str]] = []

            def ask_spy(message: str, yes: str, no: str) -> bool:
                ask_calls.append((message, yes, no))
                return True

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id",
                db_value,
                file_path,
                synced_hash,
                logs,
                ask=ask_spy,
                interactive=True,
                book_label="Wuthering Heights",
            )

            # Ask should be called once with correct message
            assert len(ask_calls) == 1
            message, yes_label, no_label = ask_calls[0]
            assert 'The synopsis of "Wuthering Heights" was changed both in ebookerr' in message
            assert "book.back_cover.txt" in message
            assert yes_label == "Keep the ebookerr version"
            assert no_label == "Keep the book.back_cover.txt version"
            # Message should not contain raw book_id or "database"
            assert "book_id" not in message.lower() or "book_id" not in message
            assert "(database)" not in message
            assert "database" not in message.lower()

            # User chose keep app
            assert resolved_value is None
            assert file_path.read_text() == "db version"

    def test_an_attended_no_keeps_the_file_version(self) -> None:
        """Interactive conflict: ask returns False → keep file version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file version")

            db_value = "db version"
            synced_hash = "old_synced_hash"
            logs: list[dict] = []

            def ask_false(m: str, y: str, n: str) -> bool:
                return False

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id",
                db_value,
                file_path,
                synced_hash,
                logs,
                ask=ask_false,
                interactive=True,
                book_label="Wuthering Heights",
            )

            # User chose keep file
            assert resolved_value == "file version"
            assert len(logs) == 1
            assert logs[0]["level"] == "warning"
            assert "kept the file version" in logs[0]["message"]

    def test_bytes_first_sync_adopts_the_cover_file(self) -> None:
        """First sync for cover: synced_hash=None, both present, differ → adopt file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.cover.png"
            file_path.write_bytes(b"file cover")

            db_file = Path(tmpdir) / "db_cover.png"
            db_file.write_bytes(b"db cover")

            synced_hash = None
            logs: list[dict] = []

            resolved_path, resolved_hash, resolved_mtime = _MODULE.merge_bytes(
                "book_id",
                str(db_file),
                file_path,
                synced_hash,
                logs,
                interactive=False,
                book_label="Test Book",
            )

            # First sync: file is adopted
            assert resolved_path == str(file_path)
            assert resolved_hash is not None
            assert resolved_mtime is not None
            assert len(logs) == 1
            assert "first sync — adopted the sidecar cover" in logs[0]["message"]

    def test_bytes_unattended_conflict_keeps_the_app_cover(self) -> None:
        """Unattended conflict for cover: keep app version, notify."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.cover.png"
            file_path.write_bytes(b"file cover")

            db_file = Path(tmpdir) / "db_cover.png"
            db_file.write_bytes(b"db cover")

            synced_hash = "old_synced_hash"
            logs: list[dict] = []
            notify_calls: list[tuple[str, str]] = []

            def notify_spy(level: str, message: str) -> None:
                notify_calls.append((level, message))

            resolved_path, resolved_hash, resolved_mtime = _MODULE.merge_bytes(
                "book_id",
                str(db_file),
                file_path,
                synced_hash,
                logs,
                ask=None,
                interactive=False,
                notify=notify_spy,
                book_label="Test Book",
            )

            # Unattended: app cover wins
            assert resolved_path is None
            assert file_path.read_bytes() == b"db cover"
            assert len(logs) == 1
            assert "kept the ebookerr version" in logs[0]["message"]

            # notify called with cover wording
            assert len(notify_calls) == 1
            assert notify_calls[0][0] == "warning"
            assert "ebookerr cover" in notify_calls[0][1]

    def test_conflict_ask_true_keeps_db_version(self) -> None:
        """Both sides changed; ask returns True → DB wins, file updated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file version")

            db_value = "db version"
            synced_hash = "old_synced_hash"
            logs: list[dict] = []

            # ask always returns True
            def ask(m: str, y: str, n: str) -> bool:
                return True

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id", db_value, file_path, synced_hash, logs, ask=ask, interactive=True
            )

            # DB version wins
            assert resolved_value is None
            assert file_path.read_text() == "db version"
            assert len(logs) == 1
            assert logs[0]["level"] == "warning"
            assert "kept the ebookerr version" in logs[0]["message"]

    def test_conflict_ask_false_keeps_file_version(self) -> None:
        """Both sides changed; ask returns False → FILE wins, file untouched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.back_cover.txt"
            file_path.write_text("file version")

            db_value = "db version"
            synced_hash = "old_synced_hash"
            logs: list[dict] = []

            # ask always returns False
            def ask(m: str, y: str, n: str) -> bool:
                return False

            resolved_value, resolved_hash, resolved_mtime = _MODULE.merge_text(
                "book_id", db_value, file_path, synced_hash, logs, ask=ask, interactive=True
            )

            # File version wins
            assert resolved_value == "file version"
            assert file_path.read_text() == "file version"
            assert len(logs) == 1
            assert logs[0]["level"] == "warning"
            assert "kept the file version" in logs[0]["message"]

    def test_conflict_bytes_ask_true_keeps_db_cover(self) -> None:
        """Both sides changed; ask returns True → DB cover wins, file updated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "book.cover.png"
            file_path.write_bytes(b"file cover")

            db_file = Path(tmpdir) / "db_cover.png"
            db_file.write_bytes(b"db cover")

            synced_hash = "old_synced_hash"
            logs: list[dict] = []

            # ask always returns True
            def ask(m: str, y: str, n: str) -> bool:
                return True

            resolved_path, resolved_hash, resolved_mtime = _MODULE.merge_bytes(
                "book_id", str(db_file), file_path, synced_hash, logs, ask=ask, interactive=True
            )

            # DB cover wins
            assert resolved_path is None
            assert file_path.read_bytes() == b"db cover"
            assert len(logs) == 1
            assert logs[0]["level"] == "warning"
            assert "kept the ebookerr version" in logs[0]["message"]


class TestNoT2ISync:
    """Tests for removal of T2I prompt syncing."""

    def test_module_has_no_t2i_processor(self) -> None:
        """The _process_t2i function has been removed from the module."""
        assert not hasattr(_MODULE, "_process_t2i")

    def test_process_book_ignores_a_t2i_sidecar(self) -> None:
        """process_book ignores T2I.txt file and doesn't return T2I keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Seed a book directory with T2I.txt
            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("a prompt")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            result = _MODULE.process_book(book, lib_root)

            # Returned patch (if any) should have no T2I keys
            if result is not None:
                assert "t2i_prompt" not in result.get("fields", {})
                assert "t2i_prompt" not in result.get("custom_values", {})
                # Check for any key starting with "t2i" in custom_values
                for key in result.get("custom_values", {}):
                    assert not key.startswith("t2i"), f"Found T2I key in result: {key}"

            # File should still exist untouched
            assert t2i_file.exists()
            assert t2i_file.read_text() == "a prompt"

    def test_delete_still_removes_the_t2i_sidecar(self) -> None:
        """handle_delete still removes T2I.txt when delete_on_book_delete is enabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Seed a book directory with T2I.txt
            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("a prompt")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            _MODULE.handle_delete(book, lib_root)

            # File should be deleted
            assert not t2i_file.exists()

    def test_delete_logs_the_sidecar_count(self) -> None:
        """handle_delete logs the count of deleted sidecar files including T2I.txt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Seed a book directory with T2I.txt
            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("a prompt")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            logs: list[dict] = []
            _MODULE.handle_delete(book, lib_root, logs)

            # Should have logged deletion with "deleted" and "sidecar file(s) on BookDeleted"
            delete_logs = [log for log in logs if "deleted" in log["message"]]
            assert len(delete_logs) > 0
            assert "sidecar file(s) on BookDeleted" in delete_logs[0]["message"]


class TestBookDeleted:
    """Tests for BookDeleted event handling."""

    def test_book_deleted_unlinks_sidecars(self) -> None:
        """BookDeleted event unlinks all sidecar files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create sidecar files
            synopsis_file = lib_root / dir_part / "book.back_cover.txt"
            synopsis_file.write_text("synopsis")

            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("prompt")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover_data")

            # Create asset file
            asset_file = lib_root / dir_part / "book.cover1.png"
            asset_file.write_bytes(b"candidate")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [
                    {
                        "kind": "cover_candidate",
                        "name": "book.cover1.png",
                        "path": "books/book.cover1.png",
                    }
                ],
            }

            _MODULE.handle_delete(book, lib_root)

            # All files should be deleted
            assert not synopsis_file.exists()
            assert not t2i_file.exists()
            assert not cover_file.exists()
            assert not asset_file.exists()

    def test_book_deleted_unlinks_legacy_and_back_cover(self) -> None:
        """BookDeleted unlinks both .back_cover.txt and legacy .synopsis.txt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create both sidecar files
            back_cover_file = lib_root / dir_part / "book.back_cover.txt"
            back_cover_file.write_text("back cover")

            legacy_file = lib_root / dir_part / "book.synopsis.txt"
            legacy_file.write_text("legacy")

            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("prompt")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover_data")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            _MODULE.handle_delete(book, lib_root)

            # Both synopsis files should be deleted
            assert not back_cover_file.exists()
            assert not legacy_file.exists()
            assert not t2i_file.exists()
            assert not cover_file.exists()

    def test_book_deleted_ignore_missing_files(self) -> None:
        """BookDeleted gracefully handles missing files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            result = _MODULE.handle_delete(book, lib_root)

            # Should succeed even with no files (returns None)
            assert result is None

    def test_book_deleted_never_unlinks_a_store_path(self, tmp_path: Path) -> None:
        """handle_delete never unlinks a stored row's core-store path; only its sidecar."""
        dir_part = "books"
        (tmp_path / dir_part).mkdir()

        store_file = tmp_path / "store.png"
        store_file.write_bytes(b"stored bytes")

        sidecar = tmp_path / dir_part / "book.c.png"
        sidecar.write_bytes(b"sidecar bytes")

        book = {
            "book_id": "test_book",
            "output_filename": "books/book.epub",
            "assets": [
                {
                    "kind": "cover_candidate",
                    "name": "book.c.png",
                    "namespace": "file_meta_sync",
                    "storage": "store",
                    "path": str(store_file),
                    "meta": {"source_file": "book.c.png"},
                }
            ],
        }

        _MODULE.handle_delete(book, tmp_path)

        assert store_file.exists()
        assert not sidecar.exists()

    def test_book_deleted_setting_absent_deletes_files(self) -> None:
        """BookDeleted with no settings key defaults to delete (true)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create sidecar files
            synopsis_file = lib_root / dir_part / "book.back_cover.txt"
            synopsis_file.write_text("synopsis")

            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("prompt")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            request = {
                "op": "enrich",
                "event": "BookDeleted",
                "request": {
                    "library_root": str(lib_root),
                    "books": [book],
                },
            }

            logs: list[dict] = []
            result = _MODULE._process_books(request, lib_root, logs)

            # Should return empty result
            assert result == []

            # Files should be deleted (default true)
            assert not synopsis_file.exists()
            assert not t2i_file.exists()

    def test_book_deleted_setting_true_deletes_files(self) -> None:
        """BookDeleted with setting true deletes files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create sidecar files
            synopsis_file = lib_root / dir_part / "book.back_cover.txt"
            synopsis_file.write_text("synopsis")

            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("prompt")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            request = {
                "op": "enrich",
                "event": "BookDeleted",
                "request": {
                    "library_root": str(lib_root),
                    "settings": {"delete_on_book_delete": True},
                    "books": [book],
                },
            }

            logs: list[dict] = []
            result = _MODULE._process_books(request, lib_root, logs)

            # Should return empty result
            assert result == []

            # Files should be deleted
            assert not synopsis_file.exists()
            assert not t2i_file.exists()

    def test_book_deleted_setting_false_keeps_files(self) -> None:
        """BookDeleted with setting false keeps files and logs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create sidecar files
            synopsis_file = lib_root / dir_part / "book.back_cover.txt"
            synopsis_file.write_text("synopsis")

            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("prompt")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover_data")

            # Create asset file
            asset_file = lib_root / dir_part / "book.cover1.png"
            asset_file.write_bytes(b"candidate")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [
                    {
                        "kind": "cover_candidate",
                        "name": "book.cover1.png",
                        "path": "books/book.cover1.png",
                    }
                ],
            }

            request = {
                "op": "enrich",
                "event": "BookDeleted",
                "request": {
                    "library_root": str(lib_root),
                    "settings": {"delete_on_book_delete": False},
                    "books": [book],
                },
            }

            logs: list[dict] = []
            result = _MODULE._process_books(request, lib_root, logs)

            # Should return empty result
            assert result == []

            # Files should still exist
            assert synopsis_file.exists()
            assert t2i_file.exists()
            assert cover_file.exists()
            assert asset_file.exists()

            # Should have logged the info message
            assert len(logs) == 1
            assert logs[0]["level"] == "info"
            assert "sidecar files kept" in logs[0]["message"]


class TestPurgeAction:
    """Tests for purge_cover_candidates action."""

    def test_purge_action_unlinks_candidates(self) -> None:
        """ui_context action purge_cover_candidates unlinks candidate and cover files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create candidate and cover files
            candidate_file = lib_root / dir_part / "book.cover1.png"
            candidate_file.write_bytes(b"candidate")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [
                    {
                        "kind": "cover_candidate",
                        "name": "book.cover1.png",
                        "path": "books/book.cover1.png",
                    }
                ],
                "custom_values": {},
            }

            deletes = _MODULE.handle_purge_action(book, lib_root)

            # Files should be deleted
            assert not candidate_file.exists()
            assert not cover_file.exists()
            # Registry rows already gone, no asset writes returned
            assert deletes == []

    def test_process_books_wraps_purge_deletes_in_book_patch(self) -> None:
        """purge_cover_candidates action does not return asset writes (registry rows gone)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create a cover candidate file
            candidate_file = lib_root / dir_part / "book.cover1.png"
            candidate_file.write_bytes(b"candidate")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [
                    {
                        "kind": "cover_candidate",
                        "name": "book.cover1.png",
                        "path": "books/book.cover1.png",
                    }
                ],
            }

            request = {
                "op": "enrich",
                "event": None,
                "request": {
                    "library_root": str(lib_root),
                    "ui_context": {"action": "purge_cover_candidates"},
                    "books": [book],
                },
            }

            result = _MODULE._process_books(request, lib_root, [])

            # handle_purge_action returns [] since registry rows are already deleted
            # So _process_books won't append anything to result
            assert result == []

            # File should be deleted
            assert not candidate_file.exists()

    def test_process_books_book_deleted_returns_empty_result(self) -> None:
        """BookDeleted event returns empty result and deletes sidecar files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create sidecar files
            synopsis_file = lib_root / dir_part / "book.back_cover.txt"
            synopsis_file.write_text("synopsis")

            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("prompt")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            request = {
                "op": "enrich",
                "event": "BookDeleted",
                "request": {
                    "library_root": str(lib_root),
                    "books": [book],
                },
            }

            result = _MODULE._process_books(request, lib_root, [])

            # Should return empty result
            assert result == []

            # Files should be deleted
            assert not synopsis_file.exists()
            assert not t2i_file.exists()

    def test_purge_candidates_deletes_sources_of_purged_stored_rows(self) -> None:
        """handle_purge_action(assets=...) deletes the sidecar of each purged stored row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            sidecar = lib_root / dir_part / "book.c.png"
            sidecar.write_bytes(b"sidecar")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            _MODULE.handle_purge_action(
                book,
                lib_root,
                paths=[],
                assets=[
                    {
                        "kind": "cover_candidate",
                        "name": "book.c.png",
                        "meta": {"source_file": "book.c.png"},
                    }
                ],
            )

            assert not sidecar.exists()
            assert not cover_file.exists()

    def test_purge_assets_keeps_the_cover_export(self) -> None:
        """purge_assets deletes the purged row's sidecar but keeps <stem>.cover.png."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            sidecar = lib_root / dir_part / "book.c.png"
            sidecar.write_bytes(b"sidecar")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            request = {
                "op": "enrich",
                "event": None,
                "request": {
                    "library_root": str(lib_root),
                    "settings": {},
                    "ui_context": {
                        "action": "purge_assets",
                        "paths": "[]",
                        "assets": json.dumps(
                            [
                                {
                                    "kind": "cover_candidate",
                                    "name": "book.c.png",
                                    "meta": {"source_file": "book.c.png"},
                                }
                            ]
                        ),
                    },
                    "books": [book],
                },
            }

            result = _MODULE._process_books(request, lib_root, [])

            assert result == []
            assert not sidecar.exists()
            assert cover_file.exists()

    def test_purge_refuses_a_source_with_a_directory_part(self) -> None:
        """A source_file with a directory part (path traversal) is never unlinked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            escape_file = lib_root / "escape.png"
            escape_file.write_bytes(b"should not be deleted")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            _MODULE.handle_purge_action(
                book,
                lib_root,
                paths=[],
                assets=[
                    {
                        "kind": "cover_candidate",
                        "name": "evil",
                        "meta": {"source_file": "../escape.png"},
                    }
                ],
            )

            assert escape_file.exists()


class TestPurgeDoesNotResurrectCandidates:
    """Regression tests for the owner ruling that purging must not resurrect a candidate."""

    def test_deleting_a_sidecar_sourced_candidate_does_not_resurrect_it(self) -> None:
        """Purging a sidecar-sourced stored candidate must not re-import it on the next run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            sidecar = lib_root / dir_part / "book.c.png"
            sidecar.write_bytes(b"img1")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            # 1. Discover and import the sidecar.
            result = _MODULE.process_book(book, lib_root)
            assert result is not None
            written = next(a for a in result["assets"] if "data" in a)

            # 2. Simulate the core storing it as a namespaced stored row.
            stored_asset = {
                "kind": "cover_candidate",
                "name": "book.c.png",
                "namespace": "file_meta_sync",
                "storage": "store",
                "path": "/store/x",
                "meta": written["meta"],
            }

            # 3. Simulate the user deleting it via purge_assets.
            purge_request = {
                "op": "enrich",
                "event": None,
                "request": {
                    "library_root": str(lib_root),
                    "settings": {},
                    "ui_context": {
                        "action": "purge_assets",
                        "paths": "[]",
                        "assets": json.dumps(
                            [
                                {
                                    "kind": stored_asset["kind"],
                                    "name": stored_asset["name"],
                                    "meta": stored_asset["meta"],
                                }
                            ]
                        ),
                    },
                    "books": [book],
                },
            }
            _MODULE._process_books(purge_request, lib_root, [])
            assert not sidecar.exists()

            # 4. Re-run process_book: the core has removed the stored row too.
            book["assets"] = []
            final = _MODULE.process_book(book, lib_root)
            assert final is None or "assets" not in final


class TestProcessBookAssets:
    """Tests for assets at top level of patch."""

    def test_process_book_emits_assets_at_top_level(self) -> None:
        """Assets are emitted at top level, not inside fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            books_dir = lib_root / "books"
            books_dir.mkdir()

            # Create a cover candidate file
            (books_dir / "book.cover1.png").write_bytes(b"x")

            book = {
                "book_id": "b1",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            result = _MODULE.process_book(book, lib_root)

            # Assert result is not None
            assert result is not None
            # Assert assets at top level
            assert "assets" in result
            assert len(result["assets"]) == 1
            written = result["assets"][0]
            assert written["kind"] == "cover_candidate"
            assert written["name"] == "book.cover1.png"
            assert written["media_type"] == "image/png"
            assert "data" in written
            assert "path" not in written
            # Assert assets NOT in fields
            assert "assets" not in result["fields"]

    def test_process_book_candidates_only_still_returns_patch(self) -> None:
        """Book with only candidate discovery returns patch even if no other changes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            books_dir = lib_root / "books"
            books_dir.mkdir()

            # Create a cover candidate file
            (books_dir / "book.cover1.png").write_bytes(b"x")

            book = {
                "book_id": "b1",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            result = _MODULE.process_book(book, lib_root)

            # Assert result is not None (candidates alone warrant a patch)
            assert result is not None
            # Assert fields and custom_values are empty
            assert result["fields"] == {}
            assert result["custom_values"] == {}
            # Assert assets has exactly one entry
            assert len(result["assets"]) == 1
            assert result["assets"][0]["name"] == "book.cover1.png"

    def test_process_book_nothing_to_do_returns_none(self) -> None:
        """Book with no candidates, no sidecars, no changes returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            books_dir = lib_root / "books"
            books_dir.mkdir()

            # No files at all
            book = {
                "book_id": "b1",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            result = _MODULE.process_book(book, lib_root)

            # Assert result is None
            assert result is None


class TestAskYesNo:
    """Tests for the _ask_yes_no helper."""

    def test_ask_yes_no_parses_answer(self, monkeypatch) -> None:
        """_ask_yes_no emits dialog op and reads answer from stdin."""
        import io

        # Mock stdin to return the dialog answer
        mock_stdin = MagicMock()
        mock_stdin.readline.return_value = '{"answer": true}\n'

        # Mock stdout to capture the dialog op
        mock_stdout = io.StringIO()

        monkeypatch.setattr(sys, "stdin", mock_stdin)
        monkeypatch.setattr(sys, "stdout", mock_stdout)

        result = _MODULE._ask_yes_no("Do you agree?", "Yes please", "No thanks")

        # Result should be True
        assert result is True

        # Captured stdout should contain the dialog op
        output = mock_stdout.getvalue()
        dialog_op = json.loads(output)
        assert dialog_op["op"] == "dialog"
        assert dialog_op["kind"] == "yes_no"
        assert dialog_op["message"] == "Do you agree?"
        assert dialog_op["yes"] == "Yes please"
        assert dialog_op["no"] == "No thanks"

    def test_ask_yes_no_raises_on_the_abandonment_frame(self, monkeypatch) -> None:
        """_ask_yes_no raises PromptAbandoned when the core sends the abandonment frame."""
        import io

        mock_stdin = MagicMock()
        mock_stdin.readline.return_value = '{"abandoned": true}\n'
        mock_stdout = io.StringIO()

        monkeypatch.setattr(sys, "stdin", mock_stdin)
        monkeypatch.setattr(sys, "stdout", mock_stdout)

        with pytest.raises(_MODULE.PromptAbandoned):
            _MODULE._ask_yes_no("Do you agree?", "Yes please", "No thanks")

        # The dialog op was still emitted before the abandonment was raised
        output = mock_stdout.getvalue()
        dialog_op = json.loads(output)
        assert dialog_op["op"] == "dialog"
        assert dialog_op["kind"] == "yes_no"

    def test_ask_yes_no_raises_on_a_closed_stdin(self, monkeypatch) -> None:
        """_ask_yes_no raises PromptAbandoned when stdin returns an empty line (closed)."""
        import io

        mock_stdin = MagicMock()
        mock_stdin.readline.return_value = ""
        mock_stdout = io.StringIO()

        monkeypatch.setattr(sys, "stdin", mock_stdin)
        monkeypatch.setattr(sys, "stdout", mock_stdout)

        with pytest.raises(_MODULE.PromptAbandoned):
            _MODULE._ask_yes_no("Do you agree?", "Yes please", "No thanks")


class TestBatchAbortsOnAbandonment:
    """Tests for batch-abort semantics when a prompt is abandoned mid-run (EXP-233)."""

    def test_the_batch_stops_at_the_abandoned_book(self, monkeypatch, tmp_path: Path) -> None:
        """A PromptAbandoned raised on book 2 propagates and book 3 is never processed."""
        spy = MagicMock(side_effect=[None, _MODULE.PromptAbandoned(), None])
        monkeypatch.setattr(_MODULE, "process_book", spy)

        books = [
            {"book_id": "b1", "output_filename": "books/b1.epub"},
            {"book_id": "b2", "output_filename": "books/b2.epub"},
            {"book_id": "b3", "output_filename": "books/b3.epub"},
        ]
        request = {
            "op": "enrich",
            "event": None,
            "request": {
                "library_root": str(tmp_path),
                "books": books,
            },
        }

        with pytest.raises(_MODULE.PromptAbandoned):
            _MODULE._process_books(request, tmp_path, [])

        # Book 3's process_book call never happened
        assert spy.call_count == 2


class TestEnvelopeRoundTrip:
    """Tests for full envelope processing via main()."""

    def test_envelope_round_trip(self, monkeypatch) -> None:
        """Feed a full request envelope via stdin, verify JSON response."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            request_data = {
                "op": "enrich",
                "event": "BookUpdated",
                "request": {
                    "library_root": str(lib_root),
                    "books": [
                        {
                            "book_id": "test_book",
                            "output_filename": "books/book.epub",
                            "synopsis": "db synopsis",
                            "cover_ref": None,
                            "assets": [],
                            "custom_values": {},
                        }
                    ],
                },
            }

            import io

            mock_stdin = MagicMock()
            mock_stdin.readline.return_value = json.dumps(request_data)
            mock_stdout = io.StringIO()

            monkeypatch.setattr(sys, "stdin", mock_stdin)
            monkeypatch.setattr(sys, "stdout", mock_stdout)

            _MODULE.main()

            output = mock_stdout.getvalue()
            # Find the final response (the line with "ok" key)
            lines = output.strip().split("\n")
            response_line = None
            for line in lines:
                try:
                    frame = json.loads(line)
                    if "ok" in frame:
                        response_line = line
                        break
                except json.JSONDecodeError:
                    pass

            assert response_line is not None, f"No response found in output: {output}"
            response = json.loads(response_line)

            assert response["ok"] is True
            assert isinstance(response["result"], list)
            assert isinstance(response["logs"], list)


class TestActionLogs:
    """Tests for action logging in sync operations."""

    def test_process_synopsis_write_logged(self) -> None:
        """_process_synopsis logs info entry when back_cover.txt is written."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()
            library_path = lib_root / dir_part

            # Setup: file has old content, DB has new content → file will be written
            synopsis_file = library_path / "book.back_cover.txt"
            synopsis_file.write_text("old file content")

            import hashlib

            old_hash = hashlib.sha256(b"old file content").hexdigest()

            # New DB content, file unchanged since synced_hash
            db_synopsis = "new db content"
            custom_values = {
                "synopsis.synced_hash": {
                    "value": old_hash,
                    "value_type": "string",
                }
            }

            logs: list[dict] = []

            syn_fields, syn_cv = _MODULE._process_synopsis(
                "test_book",
                db_synopsis,
                library_path,
                "book",
                custom_values,
                logs,
            )

            # Should have logged the write action
            assert len(logs) > 0
            write_logs = [log for log in logs if "back_cover.txt" in log["message"]]
            assert len(write_logs) > 0
            assert write_logs[0]["level"] == "info"
            assert "written" in write_logs[0]["message"] or "adopted" in write_logs[0]["message"]

    def test_process_book_logs_import_summary(self) -> None:
        """process_book logs the import/removal summary for a newly discovered sidecar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            books_dir = lib_root / "books"
            books_dir.mkdir()
            (books_dir / "book.cover1.png").write_bytes(b"img1")

            book = {
                "book_id": "b1",
                "output_filename": "books/book.epub",
                "synopsis": None,
                "cover_ref": None,
                "assets": [],
                "custom_values": {},
            }

            result = _MODULE.process_book(book, lib_root)

            assert result is not None
            assert {
                "level": "info",
                "message": "book: 1 candidate(s) imported, 0 removed",
            } in result["logs"]

    def test_handle_delete_logs_count(self) -> None:
        """handle_delete logs info entry with count of deleted sidecar files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create 2 sidecar files on disk
            synopsis_file = lib_root / dir_part / "book.back_cover.txt"
            synopsis_file.write_text("synopsis")

            t2i_file = lib_root / dir_part / "book.T2I.txt"
            t2i_file.write_text("prompt")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            logs: list[dict] = []
            _MODULE.handle_delete(book, lib_root, logs)

            # Should have logged deletion with count
            delete_logs = [log for log in logs if "deleted" in log["message"]]
            assert len(delete_logs) > 0
            assert delete_logs[0]["level"] == "info"
            assert "deleted 2 sidecar file" in delete_logs[0]["message"]

    def test_purge_logs_count(self) -> None:
        """handle_purge_action logs info entry with count of purged cover files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create cover candidate and cover file
            candidate_file = lib_root / dir_part / "book.cover1.png"
            candidate_file.write_bytes(b"candidate")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover")

            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            logs: list[dict] = []
            _MODULE.handle_purge_action(book, lib_root, logs, paths=["books/book.cover1.png"])

            # Should have logged purge with count
            purge_logs = [log for log in logs if "purged" in log["message"]]
            assert len(purge_logs) > 0
            assert purge_logs[0]["level"] == "info"
            assert "purged 1 candidate file(s) and 1 cover file(s)" in purge_logs[0]["message"]

    def test_purge_with_paths_unlinks_snapshotted_candidates(self) -> None:
        """handle_purge_action with paths= unlinks the snapshotted files, not book[assets]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create candidate and cover files
            candidate_file = lib_root / dir_part / "book.cover1.png"
            candidate_file.write_bytes(b"candidate")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover")

            # book["assets"] is empty, but paths are provided
            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [],
            }

            logs: list[dict] = []
            result = _MODULE.handle_purge_action(
                book, lib_root, logs, paths=["books/book.cover1.png"]
            )

            # Files should be deleted
            assert not candidate_file.exists()
            assert not cover_file.exists()

            # Result should be empty (no asset writes needed)
            assert result == []

    def test_purge_without_paths_falls_back_to_assets(self) -> None:
        """handle_purge_action without paths= falls back to reading book[assets]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create candidate and cover files
            candidate_file = lib_root / dir_part / "book.cover1.png"
            candidate_file.write_bytes(b"candidate")

            cover_file = lib_root / dir_part / "book.cover.png"
            cover_file.write_bytes(b"cover")

            # book has assets in registry
            book = {
                "book_id": "test_book",
                "output_filename": "books/book.epub",
                "assets": [
                    {
                        "kind": "cover_candidate",
                        "name": "book.cover1.png",
                        "path": "books/book.cover1.png",
                    }
                ],
            }

            logs: list[dict] = []
            result = _MODULE.handle_purge_action(book, lib_root, logs)

            # Files should be deleted
            assert not candidate_file.exists()
            assert not cover_file.exists()

            # Result should be empty (registry rows already gone)
            assert result == []


class TestProgressFrames:
    """Tests for progress frame emission."""

    def test_report_progress_writes_a_progress_frame(self, capsys) -> None:
        """_report_progress emits a fire-and-forget progress frame on stdout."""
        _MODULE._report_progress(42.5)
        captured = capsys.readouterr()
        frame = json.loads(captured.out)
        assert frame == {"op": "progress", "percent": 42.5}

    def test_process_books_reports_once_per_book(self, capsys) -> None:
        """_process_books reports progress after each book in normal branch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create three books
            books = [
                {
                    "book_id": f"b{i}",
                    "output_filename": f"books/book{i}.epub",
                    "synopsis": None,
                    "cover_ref": None,
                    "assets": [],
                    "custom_values": {},
                }
                for i in range(3)
            ]

            request = {
                "op": "enrich",
                "event": None,
                "request": {
                    "library_root": str(lib_root),
                    "books": books,
                },
            }

            _MODULE._process_books(request, lib_root, [])

            # Parse progress frames from stdout
            captured = capsys.readouterr()
            progress_frames = []
            for line in captured.out.strip().split("\n"):
                if line:
                    frame = json.loads(line)
                    if frame.get("op") == "progress":
                        progress_frames.append(frame["percent"])

            # Should have 3 progress frames with 100/3, 200/3, 100.0
            assert len(progress_frames) == 3
            assert progress_frames == pytest.approx([100 / 3, 200 / 3, 100.0])

    def test_process_books_reports_on_the_delete_branch(self, capsys) -> None:
        """_process_books reports progress on BookDeleted branch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            # Create sidecar files to avoid logging issues
            for i in range(2):
                (lib_root / dir_part / f"book{i}.back_cover.txt").write_text("text")

            books = [
                {
                    "book_id": f"b{i}",
                    "output_filename": f"books/book{i}.epub",
                    "assets": [],
                }
                for i in range(2)
            ]

            request = {
                "op": "enrich",
                "event": "BookDeleted",
                "request": {
                    "library_root": str(lib_root),
                    "settings": {"delete_on_book_delete": True},
                    "books": books,
                },
            }

            _MODULE._process_books(request, lib_root, [])

            # Parse progress frames
            captured = capsys.readouterr()
            progress_frames = []
            for line in captured.out.strip().split("\n"):
                if line:
                    frame = json.loads(line)
                    if frame.get("op") == "progress":
                        progress_frames.append(frame["percent"])

            # Should have 2 progress frames with 50.0, 100.0
            assert len(progress_frames) == 2
            assert progress_frames == pytest.approx([50.0, 100.0])

    def test_process_books_reports_on_the_purge_branch(self, capsys) -> None:
        """_process_books reports progress on purge_cover_candidates branch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            books = [
                {
                    "book_id": f"b{i}",
                    "output_filename": f"books/book{i}.epub",
                    "assets": [],
                }
                for i in range(2)
            ]

            request = {
                "op": "enrich",
                "event": None,
                "request": {
                    "library_root": str(lib_root),
                    "ui_context": {"action": "purge_cover_candidates"},
                    "books": books,
                },
            }

            _MODULE._process_books(request, lib_root, [])

            # Parse progress frames
            captured = capsys.readouterr()
            progress_frames = []
            for line in captured.out.strip().split("\n"):
                if line:
                    frame = json.loads(line)
                    if frame.get("op") == "progress":
                        progress_frames.append(frame["percent"])

            # Should have 2 progress frames with 50.0, 100.0
            assert len(progress_frames) == 2
            assert progress_frames == pytest.approx([50.0, 100.0])

    def test_process_books_with_no_books_emits_no_progress(self, capsys) -> None:
        """_process_books with empty books list emits no progress frames."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)

            request = {
                "op": "enrich",
                "event": None,
                "request": {
                    "library_root": str(lib_root),
                    "books": [],
                },
            }

            _MODULE._process_books(request, lib_root, [])

            # Parse progress frames
            captured = capsys.readouterr()
            progress_frames = []
            for line in captured.out.strip().split("\n"):
                if line:
                    try:
                        frame = json.loads(line)
                        if frame.get("op") == "progress":
                            progress_frames.append(frame["percent"])
                    except json.JSONDecodeError:
                        pass

            # Should have no progress frames
            assert len(progress_frames) == 0

    def test_delete_disabled_emits_no_progress(self, capsys) -> None:
        """_process_books on BookDeleted with delete_on_book_delete=False emits no progress."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lib_root = Path(tmpdir)
            dir_part = "books"
            (lib_root / dir_part).mkdir()

            books = [
                {
                    "book_id": f"b{i}",
                    "output_filename": f"books/book{i}.epub",
                    "assets": [],
                }
                for i in range(2)
            ]

            request = {
                "op": "enrich",
                "event": "BookDeleted",
                "request": {
                    "library_root": str(lib_root),
                    "settings": {"delete_on_book_delete": False},
                    "books": books,
                },
            }

            _MODULE._process_books(request, lib_root, [])

            # Parse progress frames
            captured = capsys.readouterr()
            progress_frames = []
            for line in captured.out.strip().split("\n"):
                if line:
                    try:
                        frame = json.loads(line)
                        if frame.get("op") == "progress":
                            progress_frames.append(frame["percent"])
                    except json.JSONDecodeError:
                        pass

            # Should have no progress frames (branch loops over nothing)
            assert len(progress_frames) == 0


class TestManifest:
    """Tests for manifest configuration."""

    def test_manifest_subscribes_epub_events(self) -> None:
        """Manifest subscribes to EpubCreated and EpubModified events."""
        manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
        with manifest_path.open("rb") as f:
            data = tomllib.load(f)

        assert set(data["events"]) == {
            "BookCreated",
            "BookUpdated",
            "BookImported",
            "BookDeleted",
            "EpubCreated",
            "EpubModified",
        }
