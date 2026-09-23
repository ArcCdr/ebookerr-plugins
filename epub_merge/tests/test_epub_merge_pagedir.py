"""Tests for page-progression-direction resolution in EPUB merge."""

from __future__ import annotations

import logging

import pytest
from epub_merge.merge.metadata import resolve_page_direction
from epub_merge.merge.model import InputBook, InputChapter, InputResource


def _book(
    index: int,
    *labels: str,
    page_direction: str = "ltr",
    resources: tuple[InputResource, ...] = (),
    title: str | None = None,
    language: str = "en",
    creators: tuple[tuple[str, str | None], ...] = (),
    contributors: tuple[tuple[str, str], ...] = (),
    subjects: tuple[str, ...] = (),
    dates: tuple[tuple[str, str], ...] = (),
    content_root: str = "OEBPS",
) -> InputBook:
    """Build a neutral InputBook with the given chapter labels and resources.

    Args:
        index: Book index.
        *labels: Chapter labels in reading order.
        page_direction: Page progression direction ("ltr", "rtl", or "").
        resources: Non-chapter resources to include.
        title: Book title; defaults to ``f"Book {index}"``.
        language: Language code.
        creators: Tuple of (name, file_as) tuples.
        contributors: Tuple of (name, role) tuples.
        subjects: Tuple of subject strings.
        dates: Tuple of (date_string, event) tuples.
        content_root: The book's content directory.

    Returns:
        An InputBook with the specified page_direction and other metadata.
    """
    chapters = tuple(
        InputChapter(
            label=label,
            href=f"{content_root}/file{i:04d}.xhtml",
            item_id=f"file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=f"<html>{label}</html>".encode(),
        )
        for i, label in enumerate(labels)
    )
    return InputBook(
        index=index,
        name=f"book{index}.epub",
        version="2.0",
        title=title if title is not None else f"Book {index}",
        creators=creators,
        contributors=contributors,
        language=language,
        identifier=f"id-{index}",
        source=None,
        rights=None,
        publisher=None,
        subjects=subjects,
        dates=dates,
        page_direction=page_direction,
        content_root=content_root,
        chapters=chapters,
        resources=resources,
        stylesheets=tuple(r.href for r in resources if r.media_type == "text/css"),
        cover_href=None,
        cover_media_type=None,
    )


def test_all_rtl() -> None:
    """All books with rtl page direction returns rtl (AT-PAGEDIR-1)."""
    books = [
        _book(0, "Chapter 1", page_direction="rtl"),
        _book(1, "Chapter 2", page_direction="rtl"),
    ]
    assert resolve_page_direction(books) == "rtl"


def test_mixed_returns_none() -> None:
    """Mixed page directions (ltr and rtl) returns None (AT-PAGEDIR-2)."""
    books = [
        _book(0, "Chapter 1", page_direction="ltr"),
        _book(1, "Chapter 2", page_direction="rtl"),
    ]
    assert resolve_page_direction(books) is None


def test_all_unspecified_returns_none() -> None:
    """All books with empty page direction returns None (AT-PAGEDIR-3)."""
    books = [_book(0, "Chapter 1", page_direction=""), _book(1, "Chapter 2", page_direction="")]
    assert resolve_page_direction(books) is None


def test_one_declared_one_blank() -> None:
    """One book declared, one unspecified (blank) returns the declared value."""
    books = [_book(0, "Chapter 1", page_direction="rtl"), _book(1, "Chapter 2", page_direction="")]
    assert resolve_page_direction(books) == "rtl"


def test_empty_book_list() -> None:
    """Empty book list returns None."""
    assert resolve_page_direction([]) is None


def test_logs_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Resolving page direction logs a DEBUG message."""
    books = [
        _book(0, "Chapter 1", page_direction="rtl"),
        _book(1, "Chapter 2", page_direction="rtl"),
    ]
    with caplog.at_level(logging.DEBUG):
        resolve_page_direction(books)

    assert any(
        "Merge page-progression-direction" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )
