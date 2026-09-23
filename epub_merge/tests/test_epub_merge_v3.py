"""Integration tests for EpubMergePlugin with EPUB3 nav-only books."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.testing import FakeContext
from epub_merge.plugin import EpubMergePlugin


def _submitted(*ids: str) -> api.ViewResult:
    """The merge view submitted with every named orphan selected, in this order."""
    return api.ViewResult(
        submitted=True, selections={"orphans": api.ViewSelection(order=ids, selected=ids)}
    )


def _make_book_view(book_id: str, title: str = "Book") -> api.BookView:
    return api.BookView(
        book_id=book_id,
        title=title,
        author="Author",
        story_url=None,
        output_filename=f"{book_id}.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
    )


def _make_item(book_id: str, epub_path: Path, title: str = "Book") -> api.EpubItem:
    return api.EpubItem(book=_make_book_view(book_id, title), epub_path=epub_path)


def test_merge_plugin_merges_three_nav_epub3_books(
    build_nav_epub: Callable[..., Path],
) -> None:
    """Merge three EPUB3 nav-only books via EpubMergePlugin.process."""
    # Build three nav-only EPUB3 books with distinct titles
    path_a = build_nav_epub(
        [("Chapter A1", "u1"), ("Chapter A2", "u2")],
        filename="a.epub",
        doc_title="Book A",
    )
    path_b = build_nav_epub(
        [("Chapter B1", "u1")],
        filename="b.epub",
        doc_title="Book B",
    )
    path_c = build_nav_epub(
        [("Chapter C1", "u1"), ("Chapter C2", "u2")],
        filename="c.epub",
        doc_title="Book C",
    )

    # Construct EpubItems for each book
    item_a = _make_item("book_a", path_a, "Book A")
    item_b = _make_item("book_b", path_b, "Book B")
    item_c = _make_item("book_c", path_c, "Book C")

    # Create a fake context that confirms the merge
    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("book_a", "book_b", "book_c")],
    )

    # Run the plugin's process method
    patches = EpubMergePlugin().process((item_a, item_b, item_c), ctx)

    # Verify the patches
    assert len(patches) == 3

    # Survivor patch for book_a
    survivor = patches[0]
    assert survivor.book_id == "book_a"
    assert survivor.fields == {"num_chapters": 5}  # A1, A2, B1, C1, C2
    assert survivor.emit_followup is False
    assert survivor.delete is False

    # Delete patches for book_b and book_c, superseded by the survivor
    assert patches[1] == api.BookPatch(book_id="book_b", delete=True, superseded_by="book_a")
    assert patches[2] == api.BookPatch(book_id="book_c", delete=True, superseded_by="book_a")

    # Verify the merged EPUB opens and contains all chapters in order
    doc = EpubDocument.open(path_a)
    nav_labels = [p.label for p in doc.ncx.nav_points()]
    assert nav_labels == [
        "Chapter A1",
        "Chapter A2",
        "Chapter B1",
        "Chapter C1",
        "Chapter C2",
    ]


def test_merge_plugin_declined_is_noop(
    build_nav_epub: Callable[..., Path],
) -> None:
    """If user declines merge, no changes are made."""
    # Build two nav-only EPUB3 books
    path_a = build_nav_epub(
        [("Chapter A1", "u1")],
        filename="a.epub",
        doc_title="Book A",
    )
    path_b = build_nav_epub(
        [("Chapter B1", "u1")],
        filename="b.epub",
        doc_title="Book B",
    )

    # Record the original bytes of path_a
    original_bytes = path_a.read_bytes()

    # Construct EpubItems
    item_a = _make_item("book_a", path_a, "Book A")
    item_b = _make_item("book_b", path_b, "Book B")

    # Create a context that declines the merge
    ctx = FakeContext(mode=api.InvocationMode.HEADED)

    # Run the plugin's process method
    patches = EpubMergePlugin().process((item_a, item_b), ctx)

    # Verify no patches were returned
    assert patches == []

    # Verify path_a is byte-identical to before
    assert path_a.read_bytes() == original_bytes


def test_a_real_merge_keeps_the_record_title_when_the_file_title_differs(
    build_nav_epub: Callable[..., Path],
) -> None:
    """EXP-205: merge never adopts the file's title; record title is master.

    When an EPUB's dc:title differs from the record's title and
    rewrite_book_title is False (default), the survivor patch does not
    include a title field, so the record title is never changed.
    """
    # Build the survivor EPUB with a file title that differs from its record title
    path_a = build_nav_epub(
        [("Chapter 1", "u1"), ("Chapter 2", "u2")],
        filename="a.epub",
        doc_title="Some Other Story",  # File title
    )
    path_b = build_nav_epub(
        [("Chapter 3", "u1")],
        filename="b.epub",
        doc_title="Book B",
    )

    # Construct EpubItems with a record title different from the file title
    item_a = _make_item("book_a", path_a, "Book A")  # Record title is "Book A"
    item_b = _make_item("book_b", path_b, "Book B")

    # Create a context that confirms the merge
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("book_a", "book_b")])

    # Run the plugin with default settings (rewrite_book_title=False)
    patches = EpubMergePlugin().process((item_a, item_b), ctx)

    # Verify the survivor patch does NOT include a title field
    # The record title "Book A" should be preserved, not adopted from the file
    assert len(patches) == 2
    survivor_patch = patches[0]
    assert survivor_patch.book_id == "book_a"
    assert "title" not in survivor_patch.fields
    assert survivor_patch.fields == {"num_chapters": 3}
