"""Tests for duplicate chapter number reporting during EPUB merge.

When merging two EPUBs with overlapping chapter numbers, the merge succeeds
but reports the collision via outcome.duplicate_numbers, warning logs, and
user alerts. This module validates that the merge never refuses an overlap
(R17) but always reports what happened.
"""

from __future__ import annotations

import logging

import pytest
from epub_merge.merge.merge import merge_epubs


def test_a_clean_merge_reports_no_duplicates(
    build_epub: object,
) -> None:
    """Merging books numbered 1–3 and 4–6 yields duplicate_numbers == ()."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Chapter 4", "u4"), ("Chapter 5", "u5"), ("Chapter 6", "u6")],
        filename="b.epub",
        doc_title="Book B",
    )

    outcome = merge_epubs(path1, [path2])

    assert outcome.duplicate_numbers == ()


def test_an_overlapping_merge_still_merges(
    build_epub: object,
) -> None:
    """Merging 1–3 and 3–5 succeeds; the target file exists and reopens."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Chapter 3", "u3"), ("Chapter 4", "u4"), ("Chapter 5", "u5")],
        filename="b.epub",
        doc_title="Book B",
    )

    outcome = merge_epubs(path1, [path2])

    # The merge succeeds and produces a file that can be reopened
    assert outcome.chapter_count == 6
    assert path1.exists()


def test_an_overlapping_merge_names_the_collision(
    build_epub: object,
) -> None:
    """Merging 1–3 and 3–5 yields duplicate_numbers == ("3",)."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Chapter 3", "u3"), ("Chapter 4", "u4"), ("Chapter 5", "u5")],
        filename="b.epub",
        doc_title="Book B",
    )

    outcome = merge_epubs(path1, [path2])

    assert outcome.duplicate_numbers == ("3",)


def test_several_collisions_are_all_named(
    build_epub: object,
) -> None:
    """Merging 1–5 and 3–7 yields ("3", "4", "5"), sorted."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [
            ("Chapter 1", "u1"),
            ("Chapter 2", "u2"),
            ("Chapter 3", "u3"),
            ("Chapter 4", "u4"),
            ("Chapter 5", "u5"),
        ],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [
            ("Chapter 3", "u3"),
            ("Chapter 4", "u4"),
            ("Chapter 5", "u5"),
            ("Chapter 6", "u6"),
            ("Chapter 7", "u7"),
        ],
        filename="b.epub",
        doc_title="Book B",
    )

    outcome = merge_epubs(path1, [path2])

    assert outcome.duplicate_numbers == ("3", "4", "5")


def test_a_range_overlap_is_detected(
    build_epub: object,
) -> None:
    """A chapter numbered "1-6" merged with one numbered "5" yields ("5",)."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("Chapter 1-6", "u1")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Chapter 5", "u5")],
        filename="b.epub",
        doc_title="Book B",
    )

    outcome = merge_epubs(path1, [path2])

    assert outcome.duplicate_numbers == ("5",)


def test_unnumbered_chapters_never_collide(
    build_epub: object,
) -> None:
    """Merging two books whose chapters are all unnumbered yields ()."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("Prologue", "u1"), ("Interlude", "u2"), ("Epilogue", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Prologue", "u3"), ("Interlude", "u4"), ("Epilogue", "u5")],
        filename="b.epub",
        doc_title="Book B",
    )

    outcome = merge_epubs(path1, [path2])

    assert outcome.duplicate_numbers == ()


def test_an_identical_number_within_one_input_is_reported(
    build_epub: object,
) -> None:
    """A single input already containing two chapter 2s yields ("2",)."""
    factory = build_epub  # type: ignore[assignment]
    # Create one book with two chapters labeled "Chapter 2" and one numbered chapter
    path1 = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 2", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Chapter 4", "u4")],
        filename="b.epub",
        doc_title="Book B",
    )

    outcome = merge_epubs(path1, [path2])

    assert outcome.duplicate_numbers == ("2",)


def test_the_overlap_is_logged(
    build_epub: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At WARNING, one record matching 'produced 1 duplicated chapter number(s)'."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Chapter 3", "u3"), ("Chapter 4", "u4"), ("Chapter 5", "u5")],
        filename="b.epub",
        doc_title="Book B",
    )

    with caplog.at_level(logging.WARNING, logger="epub_merge.merge.merge"):
        merge_epubs(path1, [path2])

    assert "produced 1 duplicated chapter number(s) in" in caplog.text


def test_a_clean_merge_logs_no_overlap_warning(
    build_epub: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zero WARNING records mention 'duplicated chapter number'."""
    factory = build_epub  # type: ignore[assignment]
    path1 = factory(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        filename="a.epub",
        doc_title="Book A",
    )
    path2 = factory(
        [("Chapter 4", "u4"), ("Chapter 5", "u5"), ("Chapter 6", "u6")],
        filename="b.epub",
        doc_title="Book B",
    )

    with caplog.at_level(logging.WARNING, logger="epub_merge.merge.merge"):
        merge_epubs(path1, [path2])

    # Check that no WARNING records mention the duplicate warning
    warning_records = [r for r in caplog.records if "duplicated chapter number" in r.message]
    assert len(warning_records) == 0
