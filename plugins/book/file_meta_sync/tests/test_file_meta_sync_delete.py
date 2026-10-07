"""Tests for BookDeleted cleanup in file_meta_sync plugin."""

from __future__ import annotations

from pathlib import Path

from file_meta_sync.plugin import handle_delete


def test_book_deleted_prunes_the_folders_it_empties(tmp_path: Path) -> None:
    """After deleting a book's sidecars, empty parent folders up to root are removed."""
    root = tmp_path / "library"
    folder = root / "Jane Doe" / "Example Series"
    folder.mkdir(parents=True)
    (folder / "Example Title.back_cover.txt").write_text("s")
    handle_delete(
        {"output_filename": "Jane Doe/Example Series/Example Title.epub", "assets": []},
        root,
        [],
    )
    assert not (root / "Jane Doe").exists()
    assert root.is_dir()


def test_book_deleted_keeps_a_folder_with_another_book(tmp_path: Path) -> None:
    """Folders containing other books are kept even after deleting a book's sidecars."""
    root = tmp_path / "library"
    folder = root / "Jane Doe" / "Example Series"
    folder.mkdir(parents=True)
    (folder / "Example Title.back_cover.txt").write_text("s")
    (folder / "Other.epub").write_bytes(b"")
    handle_delete(
        {"output_filename": "Jane Doe/Example Series/Example Title.epub", "assets": []},
        root,
        [],
    )
    assert not (folder / "Example Title.back_cover.txt").exists()
    assert folder.is_dir()


def test_book_deleted_without_sidecars_touches_no_folder(tmp_path: Path) -> None:
    """When a book has no sidecars, folders are left untouched."""
    root = tmp_path / "library"
    folder = root / "Jane Doe" / "Example Series"
    folder.mkdir(parents=True)
    handle_delete(
        {"output_filename": "Jane Doe/Example Series/Example Title.epub", "assets": []},
        root,
        [],
    )
    assert folder.is_dir()
