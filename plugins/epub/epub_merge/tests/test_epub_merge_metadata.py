"""Tests for EPUB merge bibliographic metadata strategies."""

from __future__ import annotations

import logging

import pytest
from epub_merge.merge.metadata import (
    merge_contributors,
    merge_creators,
    merge_subjects,
    resolve_language,
    rewrite_book_title,
)
from epub_merge.merge.model import InputBook, PlannedChapter
from epub_merge.plugin import EpubMergePlugin


def _book(
    index: int,
    name: str = "book.epub",
    title: str = "Book",
    creators: tuple[tuple[str, str | None], ...] = (),
    contributors: tuple[tuple[str, str], ...] = (),
    language: str = "en",
    subjects: tuple[str, ...] = (),
    dates: tuple[tuple[str, str], ...] = (),
) -> InputBook:
    """Build a minimal InputBook for testing metadata merge functions.

    Args:
        index: Book index.
        name: Source file name.
        title: Book title.
        creators: Tuple of (name, opf:file-as) tuples.
        contributors: Tuple of (name, opf:role) tuples.
        language: Language code.
        subjects: Tuple of subject strings.
        dates: Tuple of (date_string, event) tuples.

    Returns:
        An InputBook with the given metadata and empty content.
    """
    return InputBook(
        index=index,
        name=name,
        version="2.0",
        title=title,
        creators=creators,
        contributors=contributors,
        language=language,
        identifier=f"id-{index}",
        source=None,
        rights=None,
        publisher=None,
        subjects=subjects,
        dates=dates,
        page_direction="",
        content_root="",
        chapters=(),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )


class TestMergeCreators:
    """Tests for merge_creators function."""

    def test_merge_creators_dedups_by_name(self) -> None:
        """Creators are de-duplicated by name; later occurrences are skipped."""
        books = [
            _book(0, creators=(("Tefler", None),)),
            _book(1, creators=(("Tefler", None), ("Ann", None))),
        ]
        result = merge_creators(books)
        assert result == (("Tefler", None), ("Ann", None))

    def test_merge_creators_upgrades_file_as(self) -> None:
        """A later None file_as is upgraded by a non-None occurrence."""
        books = [
            _book(0, creators=(("Tefler", None),)),
            _book(1, creators=(("Tefler", "Tefler, T."),)),
        ]
        result = merge_creators(books)
        assert result == (("Tefler", "Tefler, T."),)

    def test_merge_creators_keeps_first_file_as(self) -> None:
        """When the first occurrence has a non-None file_as, later ones are ignored."""
        books = [
            _book(0, creators=(("Tefler", "A"),)),
            _book(1, creators=(("Tefler", "B"),)),
        ]
        result = merge_creators(books)
        assert result == (("Tefler", "A"),)

    def test_merge_creators_skips_blank(self) -> None:
        """Creators with blank names are not included in the result."""
        books = [
            _book(0, creators=(("", None), ("Ann", None))),
        ]
        result = merge_creators(books)
        assert result == (("Ann", None),)


class TestMergeContributors:
    """Tests for merge_contributors function."""

    def test_merge_contributors_dedups_by_name_and_role(self) -> None:
        """Contributors are de-duplicated by (name, role) tuple."""
        books = [
            _book(0, contributors=(("Ann", "trl"),)),
            _book(1, contributors=(("Ann", "trl"), ("Ann", "edt"))),
        ]
        result = merge_contributors(books)
        assert result == (("Ann", "trl"), ("Ann", "edt"))

    def test_merge_contributors_across_all_books(self) -> None:
        """All distinct contributors from all books are included."""
        books = [
            _book(0, contributors=(("Alice", "trl"),)),
            _book(1, contributors=(("Bob", "trl"),)),
            _book(2, contributors=(("Charlie", "edt"),)),
        ]
        result = merge_contributors(books)
        assert result == (("Alice", "trl"), ("Bob", "trl"), ("Charlie", "edt"))

    def test_merge_contributors_skips_blank(self) -> None:
        """Contributors with blank names are not included."""
        books = [
            _book(0, contributors=(("", "trl"), ("Ann", "trl"))),
        ]
        result = merge_contributors(books)
        assert result == (("Ann", "trl"),)


class TestMergeSubjects:
    """Tests for merge_subjects function."""

    def test_merge_subjects_dedups_preserving_order(self) -> None:
        """Subjects are de-duplicated preserving first-seen order."""
        books = [
            _book(0, subjects=("Erotica", "Mind Control")),
            _book(1, subjects=("Mind Control", "In-Progress")),
        ]
        result = merge_subjects(books)
        assert result == ("Erotica", "Mind Control", "In-Progress")

    def test_merge_subjects_skips_blank(self) -> None:
        """Subject strings that are blank are not included."""
        books = [
            _book(0, subjects=("", "Erotica", "Mind Control")),
        ]
        result = merge_subjects(books)
        assert result == ("Erotica", "Mind Control")


class TestResolveLanguage:
    """Tests for resolve_language function."""

    def test_resolve_language_all_agree(self, caplog: pytest.LogCaptureFixture) -> None:
        """When all books agree on language, no warning is logged."""
        books = [_book(0, language="en"), _book(1, language="en"), _book(2, language="en")]
        with caplog.at_level(logging.WARNING):
            result = resolve_language(books, override=None)

        assert result == "en"
        assert not any("Merge language mismatch" in record.message for record in caplog.records)

    def test_resolve_language_mismatch_uses_survivor_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When languages mismatch, survivor's language is used and a warning is logged."""
        books = [_book(0, language="en"), _book(1, language="fr")]
        with caplog.at_level(logging.WARNING):
            result = resolve_language(books, override=None)

        assert result == "en"
        assert any(
            "Merge language mismatch" in record.message
            and "en" in record.message
            and "fr" in record.message
            for record in caplog.records
        )

    def test_resolve_language_override_wins_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When override is set, it is used and no warning is logged."""
        books = [_book(0, language="en"), _book(1, language="fr")]
        with caplog.at_level(logging.WARNING):
            result = resolve_language(books, override="de")

        assert result == "de"
        assert not any("Merge language mismatch" in record.message for record in caplog.records)

    def test_resolve_language_defaults_to_en(self) -> None:
        """When all books have blank language, default to 'en'."""
        books = [_book(0, language=""), _book(1, language="")]
        result = resolve_language(books, override=None)
        assert result == "en"

    def test_resolve_language_ignores_blank_inputs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Blank language values from books are ignored when computing mismatch."""
        books = [_book(0, language="en"), _book(1, language="")]
        with caplog.at_level(logging.WARNING):
            result = resolve_language(books, override=None)

        assert result == "en"
        assert not any("Merge language mismatch" in record.message for record in caplog.records)


class TestRewriteBookTitle:
    """Tests for rewrite_book_title idempotence across merges."""

    def test_rewrite_book_title_is_idempotent_across_merges(self) -> None:
        """Feeding back a merged title should not accumulate stems."""
        chapters_1_2 = (
            PlannedChapter(
                label="Ch 1",
                filename="c1.xhtml",
                item_id="c1",
                number=1,
                book_index=0,
                source_href="c1.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 2",
                filename="c2.xhtml",
                item_id="c2",
                number=2,
                book_index=0,
                source_href="c2.xhtml",
                xhtml=b"",
            ),
        )
        chapters_1_4 = (
            PlannedChapter(
                label="Ch 1",
                filename="c1.xhtml",
                item_id="c1",
                number=1,
                book_index=0,
                source_href="c1.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 2",
                filename="c2.xhtml",
                item_id="c2",
                number=2,
                book_index=0,
                source_href="c2.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 3",
                filename="c3.xhtml",
                item_id="c3",
                number=3,
                book_index=0,
                source_href="c3.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 4",
                filename="c4.xhtml",
                item_id="c4",
                number=4,
                book_index=0,
                source_href="c4.xhtml",
                xhtml=b"",
            ),
        )

        # First merge: "The Senator's Daughter 1-2"
        result = rewrite_book_title("The Senator's Daughter 1-2", chapters_1_2)
        assert result == "The Senator's Daughter 1-2"

        # Second merge: feed back the result with expanded chapters
        result = rewrite_book_title(result, chapters_1_4)
        assert result == "The Senator's Daughter 1-4"

    def test_rewrite_book_title_does_not_accumulate_stems(self) -> None:
        """Multiple merges should not accumulate stems in the title."""
        # Build chapter stubs for 1, 2, 3, 4
        chapters_1_2 = (
            PlannedChapter(
                label="Ch 1",
                filename="c1.xhtml",
                item_id="c1",
                number=1,
                book_index=0,
                source_href="c1.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 2",
                filename="c2.xhtml",
                item_id="c2",
                number=2,
                book_index=0,
                source_href="c2.xhtml",
                xhtml=b"",
            ),
        )
        chapters_1_3 = (
            PlannedChapter(
                label="Ch 1",
                filename="c1.xhtml",
                item_id="c1",
                number=1,
                book_index=0,
                source_href="c1.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 2",
                filename="c2.xhtml",
                item_id="c2",
                number=2,
                book_index=0,
                source_href="c2.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 3",
                filename="c3.xhtml",
                item_id="c3",
                number=3,
                book_index=0,
                source_href="c3.xhtml",
                xhtml=b"",
            ),
        )
        chapters_1_4 = (
            PlannedChapter(
                label="Ch 1",
                filename="c1.xhtml",
                item_id="c1",
                number=1,
                book_index=0,
                source_href="c1.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 2",
                filename="c2.xhtml",
                item_id="c2",
                number=2,
                book_index=0,
                source_href="c2.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 3",
                filename="c3.xhtml",
                item_id="c3",
                number=3,
                book_index=0,
                source_href="c3.xhtml",
                xhtml=b"",
            ),
            PlannedChapter(
                label="Ch 4",
                filename="c4.xhtml",
                item_id="c4",
                number=4,
                book_index=0,
                source_href="c4.xhtml",
                xhtml=b"",
            ),
        )

        # Start from a single chapter title
        title = "The Senator's Daughter 1"

        # First merge to 1-2
        title = rewrite_book_title(title, chapters_1_2)
        assert title == "The Senator's Daughter 1-2"

        # Second merge to 1-3
        title = rewrite_book_title(title, chapters_1_3)
        assert title == "The Senator's Daughter 1-3"

        # Third merge to 1-4
        title = rewrite_book_title(title, chapters_1_4)
        assert title == "The Senator's Daughter 1-4"

        # Verify no " 1 1" substring exists (no accumulated stems)
        assert " 1 1" not in title


class TestManifest:
    """Tests for EpubMergePlugin manifest fields."""

    def test_manifest_priority_is_fifty(self) -> None:
        """The plugin's manifest priority is exactly 50."""
        plugin = EpubMergePlugin()
        assert plugin.manifest.priority == 50
