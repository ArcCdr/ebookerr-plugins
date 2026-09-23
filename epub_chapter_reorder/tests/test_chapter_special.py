"""Tests for sort_number, the chapter-reorder sort key that excludes special sections (EXP-127).

``sort_number`` (``epub_chapter_reorder.reorder_step.sort_number``) returns the number that
orders a chapter within its band, or ``""`` when the number belongs to a numbered special
section (interlude, bonus chapter, …) that must not be treated as a content-chapter ordinal.
"""

from __future__ import annotations

from ebookerr_sdk.domain.chapter_number import classify_special_chapter
from ebookerr_sdk.epub.chapters import ChapterRole, chapter_role
from epub_chapter_reorder.reorder_step import sort_number


def test_an_ordinary_numbered_chapter_is_unchanged() -> None:
    """Ordinary numbered chapter is content and sorts by its number."""
    assert chapter_role("Chapter 4: Neap Tide", "4") == ChapterRole.CONTENT
    assert sort_number("Chapter 4: Neap Tide", "4") == "4"


def test_authors_notes_are_back_matter_and_numbered_interludes_keep_their_band() -> None:
    """Author's notes are back matter, singular or plural; numbered interludes keep their band."""
    assert classify_special_chapter("Author's Note") == "back"
    assert classify_special_chapter("Author's Notes") == "back"
    # Numbered interludes are classified as inline, not promoted to chapter numbers
    assert chapter_role("Interlude 1", "1") == ChapterRole.CONTENT
    assert sort_number("Interlude 1", "1") == ""
