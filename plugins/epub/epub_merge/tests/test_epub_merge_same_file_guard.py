"""Tests for the same-file guard in EpubMergePlugin (EXP-249)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.testing import FakeContext
from epub_merge.plugin import EpubMergePlugin


def _make_book_view(
    book_id: str, title: str = "Book", author: str = "Author", output_filename: str | None = None
) -> api.BookView:
    return api.BookView(
        book_id=book_id,
        title=title,
        author=author,
        story_url=None,
        output_filename=output_filename,
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
    )


def _make_item(
    book_id: str,
    epub_path: Path,
    title: str = "Book",
    author: str = "Author",
    output_filename: str | None = None,
) -> api.EpubItem:
    return api.EpubItem(
        book=_make_book_view(book_id, title, author, output_filename), epub_path=epub_path
    )


def test_two_books_on_one_library_file_are_refused(tmp_path: Path) -> None:
    """Two EpubItems whose BookViews share output_filename are refused."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item(
            "b1",
            tmp_path / "a.epub",
            "Book A",
            output_filename="corvid_dreams/Salt and Circuitry (1of6).epub",
        ),
        _make_item(
            "b2",
            tmp_path / "b.epub",
            "Book B",
            output_filename="corvid_dreams/Salt and Circuitry (1of6).epub",
        ),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert len(ctx.alerts) == 1
    assert "share a file on disk" in ctx.alerts[0]


def test_the_refusal_is_logged_at_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The refusal is logged at WARNING level with the selected/distinct-file counts."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item(
            "b1",
            tmp_path / "a.epub",
            "Book A",
            output_filename="corvid_dreams/Salt and Circuitry (1of6).epub",
        ),
        _make_item(
            "b2",
            tmp_path / "b.epub",
            "Book B",
            output_filename="corvid_dreams/Salt and Circuitry (1of6).epub",
        ),
    )

    with caplog.at_level(logging.WARNING, logger="epub_merge.plugin"):
        _ = EpubMergePlugin().process(items, ctx)

    assert len(caplog.records) >= 1
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) >= 1
    assert "EPUB merge refused" in warning_records[0].message
    assert "2 selected book(s)" in warning_records[0].message
    assert "1 distinct library file(s)" in warning_records[0].message


def test_three_books_with_one_shared_pair_are_refused(tmp_path: Path) -> None:
    """Three items, two sharing a file, are all refused."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item(
            "b1",
            tmp_path / "a.epub",
            "Book A",
            output_filename="series/book_1.epub",
        ),
        _make_item(
            "b2",
            tmp_path / "b.epub",
            "Book B",
            output_filename="series/book_1.epub",  # Same file as b1
        ),
        _make_item(
            "b3",
            tmp_path / "c.epub",
            "Book C",
            output_filename="series/book_3.epub",
        ),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert len(ctx.alerts) == 1
    assert "share a file on disk" in ctx.alerts[0]


def test_books_with_distinct_files_still_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Books with distinct output_filenames proceed to the view request."""
    from epub_merge.merge import MergeOutcome

    def _recorder(
        target: Path,
        sources: list[Path],
        options: Any = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item(
            "b1",
            tmp_path / "a.epub",
            "Book A",
            output_filename="series/book_1.epub",
        ),
        _make_item(
            "b2",
            tmp_path / "b.epub",
            "Book B",
            output_filename="series/book_2.epub",
        ),
    )

    _ = EpubMergePlugin().process(items, ctx)

    # Should not have the shared-file alert
    assert not any("share a file on disk" in alert for alert in ctx.alerts)


def test_books_with_no_output_filename_are_not_treated_as_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two items with output_filename=None do not trigger the guard."""
    from epub_merge.merge import MergeOutcome

    def _recorder(
        target: Path,
        sources: list[Path],
        options: Any = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item(
            "b1",
            tmp_path / "a.epub",
            "Book A",
            output_filename=None,
        ),
        _make_item(
            "b2",
            tmp_path / "b.epub",
            "Book B",
            output_filename=None,
        ),
    )

    _ = EpubMergePlugin().process(items, ctx)

    # Should not have the shared-file alert
    assert not any("share a file on disk" in alert for alert in ctx.alerts)
