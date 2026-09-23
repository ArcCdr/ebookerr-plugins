"""Tests for the EPUB validation module."""

from __future__ import annotations

import pytest
from epub_validate.plugin import _summary


class TestSummary:
    """Tests for the _summary function."""

    @pytest.mark.pins("EXP-203")
    def test_a_multi_book_run_with_warnings_does_not_report_passed_for_all(self) -> None:
        """Multi-book run with warnings worst verdict names the count."""
        result = _summary(100, 0, "warnings", None, worst_count=33)
        assert result == "EPUB validation passed for all 100 book(s) — 33 with warnings"

    def test_a_multi_book_run_that_is_all_valid_still_reads_as_a_clean_pass(self) -> None:
        """Multi-book run with all valid reads as clean pass without naming count."""
        result = _summary(3, 0, "valid")
        assert result == "EPUB validation passed for all 3 book(s)"

    def test_a_multi_book_run_with_an_unreadable_file_names_it(self) -> None:
        """Multi-book run with unreadable worst verdict names it."""
        result = _summary(5, 0, "unreadable", None, worst_count=2)
        assert result == "EPUB validation passed for all 5 book(s) — 2 unreadable"

    def test_the_fallback_suffix_still_reaches_the_warnings_summary(self) -> None:
        """Fallback suffix appends correctly to multi-book warnings summary."""
        result = _summary(4, 0, "warnings", "not found", worst_count=1)
        assert (
            result
            == "EPUB validation passed for all 4 book(s) — 1 with warnings — external checker "
            "not found, used the built-in checker"
        )

    def test_the_single_book_and_error_branches_are_unchanged(self) -> None:
        """Single-book and error branches remain unchanged."""
        assert _summary(1, 0, "warnings") == "EPUB validation passed: warnings"
        assert _summary(4, 2, "errors") == "EPUB validation found errors in 2 of 4 book(s)"
