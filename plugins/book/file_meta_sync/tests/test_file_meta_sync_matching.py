"""Tests for sidecar candidate image matching logic (GEN-TR-8)."""

from pathlib import Path

import file_meta_sync.plugin as plugin_module
from file_meta_sync.plugin import discover_candidates


def _touch(folder: Path, *names: str) -> None:
    """Create files in folder with the given names, each containing b"x"."""
    for name in names:
        (folder / name).write_bytes(b"x")


def test_a_longer_stem_owns_its_files(tmp_path: Path) -> None:
    """An image belongs to the EPUB with the longest stem it extends."""
    _touch(tmp_path, "Book.epub", "Book 2.epub", "Book 2 cover.png", "Book.alt.png")

    # Book 2 should get Book 2 cover.png (longer stem)
    book2_candidates = discover_candidates(tmp_path, "Book 2", [])
    assert {w["name"] for w in book2_candidates} == {"Book 2 cover.png"}

    # Book should get Book.alt.png (Book 2 owns the one that extends it further)
    book_candidates = discover_candidates(tmp_path, "Book", [])
    assert {w["name"] for w in book_candidates} == {"Book.alt.png"}


def test_a_single_book_folder_takes_every_image(tmp_path: Path) -> None:
    """Every image except the export itself is a candidate in a one-EPUB folder."""
    _touch(
        tmp_path,
        "Example Title - Jane Doe.epub",
        "cover.jpg",
        "Example Title - Jane Doe.cover.png",
    )

    # Only cover.jpg is a candidate; the .cover.png export is excluded
    candidates = discover_candidates(tmp_path, "Example Title - Jane Doe", [])
    assert {w["name"] for w in candidates} == {"cover.jpg"}


def test_a_folder_without_its_epub_keeps_the_name_pattern(tmp_path: Path) -> None:
    """When no EPUB exists, keep the original pattern rule (fallback)."""
    _touch(tmp_path, "book.alt.png", "other.png")

    # Only book.alt.png matches the pattern book.*
    candidates = discover_candidates(tmp_path, "book", [])
    assert {w["name"] for w in candidates} == {"book.alt.png"}


def test_an_image_matching_no_epub_belongs_to_none(tmp_path: Path) -> None:
    """An image matching no EPUB's stem belongs to neither."""
    _touch(tmp_path, "A.epub", "B.epub", "unrelated.png")

    # unrelated.png matches neither A nor B
    candidates_a = discover_candidates(tmp_path, "A", [])
    assert {w["name"] for w in candidates_a} == set()

    candidates_b = discover_candidates(tmp_path, "B", [])
    assert {w["name"] for w in candidates_b} == set()


def test_an_oversized_image_follows_the_same_rule(tmp_path: Path, monkeypatch: any) -> None:
    """Oversized images follow the same ownership rule as regular ones."""
    monkeypatch.setattr(plugin_module, "_MAX_IMPORT_BYTES", 3)

    # Create files with 10 bytes (over the 3-byte limit)
    _touch(tmp_path, "Book.epub", "Book 2.epub")
    (tmp_path / "Book 2 cover.png").write_bytes(b"x" * 10)

    # Book 2 owns Book 2 cover.png (even though oversized)
    assert plugin_module._oversized_candidates(tmp_path, "Book 2") == 1

    # Book doesn't own it
    assert plugin_module._oversized_candidates(tmp_path, "Book") == 0
