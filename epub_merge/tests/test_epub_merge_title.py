"""Tests for EPUB merge title rewriting with chapter ranges."""

from __future__ import annotations

import logging

import pytest
from epub_merge.merge.metadata import rewrite_book_title
from epub_merge.merge.model import PlannedChapter


def _chapter(label: str, number: int | None) -> PlannedChapter:
    """Build a minimal PlannedChapter for testing title rewriting.

    Args:
        label: Human-readable chapter label.
        number: Optional chapter number.

    Returns:
        A PlannedChapter with the given label and number.
    """
    return PlannedChapter(
        label=label,
        filename=f"chapter_{number or 'intro'}.xhtml",
        item_id=f"chapter_{number or 'intro'}",
        number=number,
        book_index=0,
        source_href=f"chapter_{number or 'intro'}.xhtml",
        xhtml=b"<html></html>",
    )


class TestRewriteBookTitle:
    """Tests for rewrite_book_title function."""

    def test_worked_example(self) -> None:
        """Survivor title with chapters [None, 174-180] rewrites to "Three Square Meals 174-180"."""
        survivor_title = "Three Square Meals - Chapter 180"
        chapters = [
            _chapter("Title Page", None),
            _chapter("Chapter 174", 174),
            _chapter("Chapter 175", 175),
            _chapter("Chapter 176", 176),
            _chapter("Chapter 177", 177),
            _chapter("Chapter 180", 180),
        ]
        result = rewrite_book_title(survivor_title, chapters)
        assert result == "Three Square Meals 174-180"

    def test_separator_is_hyphen_minus(self) -> None:
        """The returned string contains U+002D HYPHEN-MINUS, not en-dash or em-dash."""
        survivor_title = "Test - Chapter 5"
        chapters = [_chapter("Chapter 5", 5), _chapter("Chapter 6", 6)]
        result = rewrite_book_title(survivor_title, chapters)
        assert "-" in result
        assert "–" not in result
        assert "—" not in result

    def test_no_zero_padding(self) -> None:
        """Chapters [7, 9] produce "… 7-9", not "07-09"."""
        survivor_title = "Story - Chapter 9"
        chapters = [_chapter("Chapter 7", 7), _chapter("Chapter 9", 9)]
        result = rewrite_book_title(survivor_title, chapters)
        assert result == "Story 7-9"

    def test_single_chapter_range(self) -> None:
        """One numbered chapter [5] produces "… 5-5"."""
        survivor_title = "Book - Chapter 5"
        chapters = [_chapter("Chapter 5", 5)]
        result = rewrite_book_title(survivor_title, chapters)
        assert result == "Book 5-5"

    def test_unnumbered_chapters_are_ignored(self) -> None:
        """Chapters [None, 3, None, 8] produces "… 3-8", ignoring the unnumbered ones."""
        survivor_title = "Series - Chapter 8"
        chapters = [
            _chapter("Intro", None),
            _chapter("Chapter 3", 3),
            _chapter("Interlude", None),
            _chapter("Chapter 8", 8),
        ]
        result = rewrite_book_title(survivor_title, chapters)
        assert result == "Series 3-8"

    def test_all_unnumbered_returns_none(self) -> None:
        """When all chapters are unnumbered, return None."""
        survivor_title = "Book"
        chapters = [
            _chapter("Prologue", None),
            _chapter("Interlude", None),
            _chapter("Epilogue", None),
        ]
        result = rewrite_book_title(survivor_title, chapters)
        assert result is None

    def test_empty_chapters_returns_none(self) -> None:
        """When chapters list is empty, return None."""
        survivor_title = "Book - Chapter 1"
        result = rewrite_book_title(survivor_title, [])
        assert result is None

    def test_stem_strips_chapter_suffix(self) -> None:
        """Survivor "AIF 36" with chapters [35, 37] produces "AIF 35-37"."""
        survivor_title = "AIF 36"
        chapters = [_chapter("Chapter 35", 35), _chapter("Chapter 37", 37)]
        result = rewrite_book_title(survivor_title, chapters)
        assert result == "AIF 35-37"

    def test_stem_falls_back_to_full_title(self) -> None:
        """When no chapter part detected, use full title with chapters [1, 3]."""
        survivor_title = "Accidental Family"
        chapters = [_chapter("Chapter 1", 1), _chapter("Chapter 3", 3)]
        result = rewrite_book_title(survivor_title, chapters)
        assert result == "Accidental Family 1-3"

    def test_logs_info_on_rewrite(self, caplog: pytest.LogCaptureFixture) -> None:
        """When title is rewritten, log at INFO level with both titles."""
        survivor_title = "Book - Chapter 5"
        chapters = [_chapter("Chapter 5", 5), _chapter("Chapter 6", 6)]
        with caplog.at_level(logging.INFO):
            result = rewrite_book_title(survivor_title, chapters)
        assert "Merge book title rewritten" in caplog.text
        assert survivor_title in caplog.text
        assert result in caplog.text

    def test_logs_debug_when_unchanged(self, caplog: pytest.LogCaptureFixture) -> None:
        """When all chapters are unnumbered, log at DEBUG level."""
        survivor_title = "Book"
        chapters = [_chapter("Prologue", None), _chapter("Epilogue", None)]
        with caplog.at_level(logging.DEBUG):
            result = rewrite_book_title(survivor_title, chapters)
        assert result is None
        assert (
            "Merge book title unchanged: no chapter in the merge set carries a number"
            in caplog.text
        )
