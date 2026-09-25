"""Tests for description, source, rights, and publisher metadata strategies."""

from __future__ import annotations

import logging

import pytest
from epub_merge.merge.metadata import build_description, survivor_value
from epub_merge.merge.model import InputBook


def _book(
    index: int,
    name: str = "book.epub",
    title: str = "Book",
    creators: tuple[tuple[str, str | None], ...] = (),
    contributors: tuple[tuple[str, str], ...] = (),
    language: str = "en",
    subjects: tuple[str, ...] = (),
    dates: tuple[tuple[str, str], ...] = (),
    source: str | None = None,
    rights: str | None = None,
    publisher: str | None = None,
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
        source: Optional source URL.
        rights: Optional rights information.
        publisher: Optional publisher name.

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
        source=source,
        rights=rights,
        publisher=publisher,
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


class TestBuildDescription:
    """Tests for build_description function."""

    def test_description_auto_generated_lines(self) -> None:
        """Three books titled A/B/C with creators X/Y/Z generate three lines."""
        books = [
            _book(0, title="A", creators=(("X", None),)),
            _book(1, title="B", creators=(("Y", None),)),
            _book(2, title="C", creators=(("Z", None),)),
        ]
        result = build_description(books, override=None)
        assert result == "A by X\nB by Y\nC by Z\n"

    def test_description_omits_by_when_no_creator(self) -> None:
        """A book with no creators contributes only the title."""
        books = [
            _book(0, title="AIF 36", creators=()),
        ]
        result = build_description(books, override=None)
        assert result == "AIF 36\n"

    def test_description_override_is_verbatim(self) -> None:
        """Override value is returned exactly, regardless of books."""
        books = [
            _book(0, title="A", creators=(("X", None),)),
            _book(1, title="B", creators=(("Y", None),)),
        ]
        result = build_description(books, override="Custom text")
        assert result == "Custom text"

    def test_description_empty_books(self) -> None:
        """An empty book list with no override yields an empty string."""
        result = build_description([], override=None)
        assert result == ""

    def test_description_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """When no override is supplied, a debug message is logged."""
        books = [
            _book(0, title="A", creators=(("X", None),)),
        ]
        with caplog.at_level(logging.DEBUG):
            result = build_description(books, override=None)

        assert result == "A by X\n"
        assert any(
            "Merge description auto-generated" in record.message for record in caplog.records
        )


class TestSurvivorValue:
    """Tests for survivor_value function."""

    def test_survivor_value_prefers_override(self) -> None:
        """Override is returned when set, regardless of survivor's value."""
        books = [
            _book(0, source="https://example.com/"),
        ]
        result = survivor_value(books, "source", "https://override/")
        assert result == "https://override/"

    def test_survivor_value_uses_survivor(self) -> None:
        """When no override, survivor's field value is used."""
        books = [
            _book(0, source="https://example.com/story"),
        ]
        result = survivor_value(books, "source", override=None)
        assert result == "https://example.com/story"

    def test_survivor_value_none_when_absent(self) -> None:
        """When survivor's field is None and no override, result is None."""
        books = [
            _book(0, source=None),
        ]
        result = survivor_value(books, "source", override=None)
        assert result is None

    def test_survivor_value_none_when_blank(self) -> None:
        """When survivor's field is blank and no override, result is None."""
        books = [
            _book(0, rights=""),
        ]
        result = survivor_value(books, "rights", override=None)
        assert result is None

    def test_survivor_value_ignores_other_books(self) -> None:
        """Only the first book's value is considered, not other books."""
        books = [
            _book(0, publisher=None),
            _book(1, publisher="literotica.com"),
        ]
        result = survivor_value(books, "publisher", override=None)
        assert result is None
