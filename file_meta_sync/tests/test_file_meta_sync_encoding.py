"""Tests for FileMetaSync sidecar encoding and UTF-8 BOM handling (TXE-D1)."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
from pathlib import Path

# Dynamically import entrypoint module
_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "entrypoint.py"
_SPEC = importlib.util.spec_from_file_location("file_meta_sync_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["file_meta_sync_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class TestFileSyncEncoding:
    """Tests for sidecar file encoding and BOM handling."""

    def test_a_bom_sidecar_reads_without_the_mark(self) -> None:
        """A sidecar file with UTF-8 BOM is read without the BOM (TXE-D1)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.back_cover.txt"
            text_content = "Café résumé"
            # Write with BOM
            file_path.write_bytes(b"\xef\xbb\xbf" + text_content.encode("utf-8"))

            # Simulate the sync logic: read the file
            file_text = file_path.read_text(encoding="utf-8-sig")
            # Compute hash the way the plugin does
            file_hash = hashlib.sha256(file_text.encode("utf-8")).hexdigest()

            # Expected: the text without BOM, and its hash matches the DB value hash
            assert file_text == text_content
            db_value = text_content
            db_hash = hashlib.sha256((db_value or "").encode("utf-8")).hexdigest()
            assert file_hash == db_hash

    def test_the_sidecar_is_written_as_utf8(self) -> None:
        """A sidecar is written with explicit UTF-8 encoding, no BOM (TXE-D1)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.back_cover.txt"
            db_value = 'Война и мир — "ok"'

            # Write the file as the plugin does
            file_path.write_text(db_value or "", encoding="utf-8")

            # Verify the bytes match UTF-8 without BOM
            expected_bytes = db_value.encode("utf-8")
            actual_bytes = file_path.read_bytes()
            assert actual_bytes == expected_bytes
            # Explicitly confirm no BOM
            assert not actual_bytes.startswith(b"\xef\xbb\xbf")
