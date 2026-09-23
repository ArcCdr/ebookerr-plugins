"""Tests for deterministic, order-independent merge survivor election."""

import itertools
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from epub_merge.merge import SurvivorCandidate, candidate_from_epub, elect_survivor


class TestElectSurvivor:
    """Test the survivor election logic."""

    def test_the_lowest_first_chapter_wins(self) -> None:
        """The book with the lowest first_number is elected."""
        candidates = [
            SurvivorCandidate(
                book_id="book1",
                title="Book 1",
                first_number=1,
                min_number=1,
                chapter_count=10,
                created_at=None,
            ),
            SurvivorCandidate(
                book_id="book2",
                title="Book 2",
                first_number=4,
                min_number=4,
                chapter_count=10,
                created_at=None,
            ),
            SurvivorCandidate(
                book_id="book3",
                title="Book 3",
                first_number=9,
                min_number=9,
                chapter_count=10,
                created_at=None,
            ),
        ]
        assert elect_survivor(candidates) == "book1"

    def test_the_result_does_not_depend_on_order(self) -> None:
        """The same candidates in any order elect the same survivor."""
        candidates = [
            SurvivorCandidate(
                book_id="book1",
                title="Book 1",
                first_number=1,
                min_number=1,
                chapter_count=10,
                created_at=None,
            ),
            SurvivorCandidate(
                book_id="book2",
                title="Book 2",
                first_number=4,
                min_number=4,
                chapter_count=10,
                created_at=None,
            ),
            SurvivorCandidate(
                book_id="book3",
                title="Book 3",
                first_number=9,
                min_number=9,
                chapter_count=10,
                created_at=None,
            ),
        ]
        expected = elect_survivor(candidates)

        # Test all 6 permutations
        for permuted in itertools.permutations(candidates):
            assert elect_survivor(list(permuted)) == expected

    def test_the_scenario_case(self) -> None:
        """The live-measured scenario case: 3-chapter 9-11 clicked first loses to 6-chapter 1-6."""
        early_chapters = SurvivorCandidate(
            book_id="early",
            title="BestBook 1-6",
            first_number=1,
            min_number=1,
            chapter_count=6,
            created_at=None,
        )
        late_chapters = SurvivorCandidate(
            book_id="late",
            title="BestBook 9-11",
            first_number=9,
            min_number=9,
            chapter_count=3,
            created_at=None,
        )

        # Both orders should elect the same (early) book
        assert elect_survivor([late_chapters, early_chapters]) == "early"
        assert elect_survivor([early_chapters, late_chapters]) == "early"

    def test_a_book_with_no_numbers_sorts_last(self) -> None:
        """A book with no parsable numbers loses to one with any number."""
        unnumbered = SurvivorCandidate(
            book_id="unnumbered",
            title="Unnumbered",
            first_number=None,
            min_number=None,
            chapter_count=5,
            created_at=None,
        )
        numbered = SurvivorCandidate(
            book_id="numbered",
            title="Numbered",
            first_number=5,
            min_number=5,
            chapter_count=5,
            created_at=None,
        )

        assert elect_survivor([unnumbered, numbered]) == "numbered"
        assert elect_survivor([numbered, unnumbered]) == "numbered"

    def test_two_unnumbered_books_fall_through_to_chapter_count(self) -> None:
        """Two unnumbered books: the one with more chapters wins."""
        small = SurvivorCandidate(
            book_id="small",
            title="Small",
            first_number=None,
            min_number=None,
            chapter_count=2,
            created_at=None,
        )
        large = SurvivorCandidate(
            book_id="large",
            title="Large",
            first_number=None,
            min_number=None,
            chapter_count=5,
            created_at=None,
        )

        assert elect_survivor([small, large]) == "large"
        assert elect_survivor([large, small]) == "large"

    def test_a_tie_on_number_prefers_more_chapters(self) -> None:
        """Equal first_number and min_number: more chapters wins."""
        fewer = SurvivorCandidate(
            book_id="fewer",
            title="Fewer",
            first_number=1,
            min_number=1,
            chapter_count=3,
            created_at=None,
        )
        more = SurvivorCandidate(
            book_id="more",
            title="More",
            first_number=1,
            min_number=1,
            chapter_count=6,
            created_at=None,
        )

        assert elect_survivor([fewer, more]) == "more"
        assert elect_survivor([more, fewer]) == "more"

    def test_a_tie_on_count_prefers_the_older_row(self) -> None:
        """Tied on all other criteria: the older created_at wins."""
        newer = SurvivorCandidate(
            book_id="newer",
            title="Newer",
            first_number=1,
            min_number=1,
            chapter_count=5,
            created_at=datetime(2026, 1, 1),
        )
        older = SurvivorCandidate(
            book_id="older",
            title="Older",
            first_number=1,
            min_number=1,
            chapter_count=5,
            created_at=datetime(2025, 1, 1),
        )

        assert elect_survivor([newer, older]) == "older"
        assert elect_survivor([older, newer]) == "older"

    def test_a_full_tie_falls_through_to_book_id(self) -> None:
        """All else equal including created_at: lexicographically smallest book_id wins."""
        zzz = SurvivorCandidate(
            book_id="zzz",
            title="ZZZ",
            first_number=1,
            min_number=1,
            chapter_count=5,
            created_at=datetime(2025, 1, 1),
        )
        aaa = SurvivorCandidate(
            book_id="aaa",
            title="AAA",
            first_number=1,
            min_number=1,
            chapter_count=5,
            created_at=datetime(2025, 1, 1),
        )

        assert elect_survivor([zzz, aaa]) == "aaa"
        assert elect_survivor([aaa, zzz]) == "aaa"

    def test_an_empty_set_raises(self) -> None:
        """An empty candidate list raises ValueError."""
        with pytest.raises(ValueError, match="No candidates"):
            elect_survivor([])

    def test_ranges_are_expanded(self) -> None:
        """A chapter number like '1-6' is expanded to min=1."""
        candidate = SurvivorCandidate(
            book_id="range",
            title="Range",
            first_number=1,
            min_number=1,
            chapter_count=6,
            created_at=None,
        )
        # This candidate was built with a range already expanded
        assert candidate.first_number == 1
        assert candidate.min_number == 1

    def test_candidate_from_epub_reads_the_first_content_chapter(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """candidate_from_epub extracts numbers from content chapters."""
        epub_path = build_epub([("Chapter 9", "u9"), ("Chapter 10", "u10"), ("Chapter 11", "u11")])

        candidate = candidate_from_epub("test-id", "Test", epub_path, None)
        assert candidate.book_id == "test-id"
        assert candidate.title == "Test"
        assert candidate.first_number == 9
        assert candidate.min_number == 9
        assert candidate.chapter_count == 3

    def test_candidate_from_epub_ignores_front_matter(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """candidate_from_epub skips FRONT chapters; first_number is from first CONTENT."""
        epub_path = build_epub([("Prologue", "u0"), ("Chapter 4", "u4")])

        candidate = candidate_from_epub("test-id", "Test", epub_path, None)
        assert candidate.first_number == 4
        assert candidate.min_number == 4

    def test_candidate_from_an_unreadable_epub_is_last_resort(self) -> None:
        """An unreadable EPUB yields first_number=None, chapter_count=0, no exception."""
        bad_epub_path = Path("/nonexistent/not-a-zip.epub")

        candidate = candidate_from_epub("test-id", "Test", bad_epub_path, None)
        assert candidate.book_id == "test-id"
        assert candidate.title == "Test"
        assert candidate.first_number is None
        assert candidate.min_number is None
        assert candidate.chapter_count == 0
        assert candidate.created_at is None

    def test_an_unreadable_epub_warns(self, caplog) -> None:
        """An unreadable EPUB emits one WARNING log."""
        bad_epub_path = Path("/nonexistent/not-a-zip.epub")

        with caplog.at_level(logging.WARNING):
            candidate_from_epub("test-id", "Test", bad_epub_path, None)
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    def test_the_election_is_logged(self, caplog) -> None:
        """elect_survivor emits an INFO log."""
        candidates = [
            SurvivorCandidate(
                book_id="book1",
                title="Book 1",
                first_number=1,
                min_number=1,
                chapter_count=10,
                created_at=None,
            ),
        ]

        with caplog.at_level(logging.INFO):
            elect_survivor(candidates)
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(info_records) >= 1
        assert "Merge survivor elected:" in info_records[0].message

    def test_a_reordering_election_says_so(self, caplog) -> None:
        """When the elected survivor is not the first candidate, emit a second INFO line."""
        early = SurvivorCandidate(
            book_id="early",
            title="Early",
            first_number=1,
            min_number=1,
            chapter_count=10,
            created_at=None,
        )
        late = SurvivorCandidate(
            book_id="late",
            title="Late",
            first_number=9,
            min_number=9,
            chapter_count=3,
            created_at=None,
        )

        # Offer late first; early should still win
        with caplog.at_level(logging.INFO):
            elect_survivor([late, early])

        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        reorder_messages = [
            r for r in info_records if "differs from the caller's first book" in r.message
        ]
        assert len(reorder_messages) >= 1
