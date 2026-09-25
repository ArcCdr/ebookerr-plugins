"""The merge plugin's own half of three unhappy flows (the session half lives in the
core's merge-flow tests)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
from ebookerr_sdk.testing import FakeContext, make_epub_item
from epub_merge.plugin import EpubMergePlugin


def _item(book_id: str, epub: Path) -> api.EpubItem:
    """A merge input carrying only what the merge reads (no shared defaults)."""
    return make_epub_item(
        epub,
        book_id=book_id,
        title=book_id,
        story_url=None,
        output_filename=f"{book_id}.epub",
        num_chapters=None,
        status=None,
    )


def _submitted(*ids: str) -> api.ViewResult:
    """The merge view submitted with every orphan selected, in this order."""
    return api.ViewResult(
        submitted=True, selections={"orphans": api.ViewSelection(order=ids, selected=ids)}
    )


def test_a_disjoint_merge_with_a_gap_counts_every_chapter(build_epub: Callable[..., Path]) -> None:
    """Chapters 1–6 and 9–11 merge into nine; the absorbed book is deleted in favour of
    the survivor."""
    a = build_epub(
        [(f"Chapter {i}", f"http://example.com/{i}") for i in range(1, 7)],
        filename="book_a.epub",
        doc_title="Book A",
    )
    b = build_epub(
        [(f"Chapter {i}", f"http://example.com/{i}") for i in range(9, 12)],
        filename="book_b.epub",
        doc_title="Book B",
    )
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("book_a", "book_b")])

    patches = EpubMergePlugin().process((_item("book_a", a), _item("book_b", b)), ctx)

    assert patches[0].fields == {"num_chapters": 9}
    assert patches[1] == api.BookPatch(book_id="book_b", delete=True, superseded_by="book_a")
    assert ctx.alerts == []


def test_an_unreadable_source_changes_nothing_and_says_why(build_epub: Callable[..., Path]) -> None:
    """A source that is not a zip stops the merge with one alert naming it; the survivor
    keeps its bytes."""
    a = build_epub(
        [("Chapter 1", "http://example.com/1"), ("Chapter 2", "http://example.com/2")],
        filename="book_a.epub",
        doc_title="Book A",
    )
    b = a.parent / "b.epub"
    b.write_bytes(b"not a zip")
    before = a.read_bytes()
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("book_a", "book_b")])

    patches = EpubMergePlugin().process((_item("book_a", a), _item("book_b", b)), ctx)

    assert patches == []
    assert ctx.alerts == [
        "Merge failed — one of the selected books could not be read: b.epub: File is not a zip file"
    ]
    assert a.read_bytes() == before


def test_a_source_with_no_content_chapters_is_absorbed_by_the_other(
    build_epub: Callable[..., Path],
) -> None:
    """A title-page-only book is the orphan; the book with chapters survives with its
    two chapters."""
    a = build_epub([], filename="book_a.epub", doc_title="Title Page Only")
    b = build_epub(
        [("Chapter 1", "http://example.com/1"), ("Chapter 2", "http://example.com/2")],
        filename="book_b.epub",
        doc_title="Book B",
    )
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("book_a", "book_b")])

    patches = EpubMergePlugin().process((_item("book_a", a), _item("book_b", b)), ctx)

    assert patches[0].book_id == "book_b"
    assert patches[0].fields == {"num_chapters": 2}
    assert patches[1] == api.BookPatch(book_id="book_a", delete=True, superseded_by="book_b")
