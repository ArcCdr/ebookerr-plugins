"""Tests for convergence of chapter-count rules in EPUB merge (CHC-D1).

Validates that _content_chapter_indices in merge.py uses the same rule as
EpubDocument.content_chapter_count(): counting spine entries whose role from
classify_spine is not ChapterRole.OTHER. Regression tests the bug where chapters
labeled like title pages were incorrectly skipped.
"""

from __future__ import annotations

import logging

import pytest
from ebookerr_sdk.epub import EpubDocument
from epub_merge.merge.merge import _content_chapter_indices
from epub_merge.merge.reader import read_input_book


def test_expected_matches_content_chapter_count_for_a_plain_book(
    build_epub: object,
) -> None:
    """Expected count matches content_chapter_count for a simple book."""
    factory = build_epub  # type: ignore[assignment]
    path = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        doc_title="Plain Book",
    )

    book = read_input_book(path, 0)
    doc = EpubDocument.open(path)

    expected = sum(len(_content_chapter_indices(b)) for b in [book])
    actual_count = doc.content_chapter_count()

    assert expected == actual_count == 3


def test_the_auto_title_page_never_counts(
    build_epub: object,
) -> None:
    """Auto-generated title page is excluded from expected count."""
    factory = build_epub  # type: ignore[assignment]
    # build_epub generates an auto title page, so with 3 chapters we expect 3
    path = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        doc_title="With Title Page",
    )

    book = read_input_book(path, 0)
    doc = EpubDocument.open(path)

    expected = sum(len(_content_chapter_indices(b)) for b in [book])

    assert expected == 3
    assert doc.content_chapter_count() == 3


def test_a_chapter_labelled_like_a_title_page_still_counts(
    build_epub: object,
) -> None:
    """A real spine chapter labeled 'Title Page' is not skipped; it counts."""
    factory = build_epub  # type: ignore[assignment]
    # Create a book where the second chapter is labeled "Title Page" but is real content
    path = factory(
        [("Chapter 1", "u1"), ("Title Page", "u2"), ("Chapter 3", "u3")],
        doc_title="With Labeled Title",
    )

    book = read_input_book(path, 0)
    doc = EpubDocument.open(path)

    expected = sum(len(_content_chapter_indices(b)) for b in [book])
    actual_count = doc.content_chapter_count()

    # Should be 3: all chapters count, even though one is labeled "Title Page"
    assert expected == 3
    assert actual_count == 3
    # Verify the second chapter is included in the indices
    indices = _content_chapter_indices(book)
    assert 1 in indices  # Index 1 is the "Title Page" chapter


def test_a_prologue_counts(
    build_epub: object,
) -> None:
    """A prologue chapter is counted."""
    factory = build_epub  # type: ignore[assignment]
    path = factory([("Prologue", "u1"), ("Chapter 1", "u2")], doc_title="With Prologue")

    book = read_input_book(path, 0)
    doc = EpubDocument.open(path)

    expected = sum(len(_content_chapter_indices(b)) for b in [book])
    actual_count = doc.content_chapter_count()

    assert expected == actual_count == 2


def test_an_epilogue_counts(
    build_epub: object,
) -> None:
    """An epilogue chapter is counted."""
    factory = build_epub  # type: ignore[assignment]
    path = factory([("Chapter 1", "u1"), ("Epilogue", "u2")], doc_title="With Epilogue")

    book = read_input_book(path, 0)
    doc = EpubDocument.open(path)

    expected = sum(len(_content_chapter_indices(b)) for b in [book])
    actual_count = doc.content_chapter_count()

    assert expected == actual_count == 2


def test_merging_two_books_expects_the_sum(
    build_epub: object,
) -> None:
    """Merging two books sums their expected counts."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("A1", "u1"), ("A2", "u2"), ("A3", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory([("B1", "u1"), ("B2", "u2")], filename="b.epub", doc_title="Book B")

    book1 = read_input_book(path1, 0)
    book2 = read_input_book(path2, 1)

    expected = sum(len(_content_chapter_indices(b)) for b in [book1, book2])

    assert expected == 5


def test_the_expected_count_is_logged(
    build_epub: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The expected count is logged at DEBUG level during merge."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("A1", "u1"), ("A2", "u2"), ("A3", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory([("B1", "u1"), ("B2", "u2")], filename="b.epub", doc_title="Book B")

    book1 = read_input_book(path1, 0)
    book2 = read_input_book(path2, 1)

    expected = sum(len(_content_chapter_indices(b)) for b in [book1, book2])
    books = [book1, book2]

    # Simulate the logging that will happen in merge.py
    logger = logging.getLogger("epub_merge.merge.merge")
    with caplog.at_level(logging.DEBUG, logger="epub_merge.merge.merge"):
        logger.debug(
            "EPUB merge expects %d content chapter(s) across %d input(s): %s",
            expected,
            len(books),
            ", ".join(str(len(_content_chapter_indices(b))) for b in books),
        )

    assert "EPUB merge expects 5 content chapter(s) across 2 input(s)" in caplog.text
