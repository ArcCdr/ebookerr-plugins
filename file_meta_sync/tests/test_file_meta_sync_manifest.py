"""Contract tests for the bundled File Metadata Sync manifest."""

from pathlib import Path

_MANIFEST = (Path(__file__).resolve().parents[1] / "manifest.toml").read_text()


def test_manifest_declares_no_t2i_custom_value() -> None:
    """Verify T2I prompt is not declared as a custom value."""
    assert "t2i_prompt" not in _MANIFEST
    assert "T2I" not in _MANIFEST


def test_manifest_declares_no_custom_values_at_all() -> None:
    """Verify no custom_values block exists."""
    assert "[[custom_values]]" not in _MANIFEST


def test_manifest_keeps_its_identity() -> None:
    """Verify core plugin identity is preserved."""
    assert 'id = "file_meta_sync"' in _MANIFEST
    assert 'type = "book"' in _MANIFEST
    assert "accepts_list = true" in _MANIFEST
    assert "headed = true" in _MANIFEST


def test_manifest_keeps_both_ui_triggers() -> None:
    """Verify both UI trigger blocks remain."""
    assert _MANIFEST.count("[[ui_triggers]]") == 2
    assert 'label = "Sync metadata file"' in _MANIFEST
    assert 'label = "Purge cover files"' in _MANIFEST


def test_manifest_keeps_the_delete_setting() -> None:
    """Verify the delete_on_book_delete setting is preserved."""
    assert 'key = "delete_on_book_delete"' in _MANIFEST


def test_manifest_description_names_only_covers_and_synopsis() -> None:
    """Verify description mentions only covers and synopsis."""
    assert (
        'description = "Syncs sidecar files (cover candidates, synopsis) with the library"'
        in _MANIFEST
    )
