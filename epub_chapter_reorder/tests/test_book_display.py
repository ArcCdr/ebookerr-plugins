"""Tests for user-facing book display strings."""

from __future__ import annotations

import pytest
from epub_chapter_reorder.book_display import display_title


def test_record_title_wins() -> None:
    """Record title takes precedence when non-blank."""
    assert display_title("Edited", epub_title="Raw") == "Edited"


def test_epub_title_used_when_record_blank() -> None:
    """EPUB title used when record title is empty string."""
    assert display_title("", epub_title="Raw") == "Raw"


def test_epub_title_used_when_record_none() -> None:
    """EPUB title used when record title is None."""
    assert display_title(None, epub_title="Raw") == "Raw"


def test_whitespace_record_title_is_blank() -> None:
    """Whitespace-only record title is treated as blank."""
    assert display_title("   ", epub_title="Raw") == "Raw"


def test_both_blank_returns_empty_string() -> None:
    """Empty string returned when both titles are blank."""
    assert display_title(None) == ""
    assert display_title("", epub_title="  ") == ""


def test_record_title_preserved_verbatim() -> None:
    """Record title returned unchanged, including whitespace."""
    assert display_title("  Padded Title  ") == "  Padded Title  "


def test_epub_title_is_keyword_only() -> None:
    """epub_title parameter must be passed by keyword."""
    with pytest.raises(TypeError):
        display_title("a", "b")  # type: ignore[call-arg]
