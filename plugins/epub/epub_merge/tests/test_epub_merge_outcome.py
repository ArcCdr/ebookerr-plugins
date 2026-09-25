"""Tests for `MergeOutcome` and its contributions field."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.epub.chapters import chapter_table
from epub_merge.merge import MergeOutcome, merge_epubs

_L = "https://example.test/s/"


def test_contributions_default_empty() -> None:
    """MergeOutcome(chapter_count=3) has contributions == ()."""
    outcome = MergeOutcome(chapter_count=3)
    assert outcome.contributions == ()


def test_contributions_sum_to_chapter_count(build_epub: Callable[..., Path]) -> None:
    """Merge three single-chapter EPUBs; assert outcome.contributions sums to chapter_count."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    outcome = merge_epubs(target, [source1, source2])

    assert outcome.contributions == (1, 1, 1)
    assert sum(outcome.contributions) == outcome.chapter_count == 3


def test_contributions_reflect_uneven_inputs(build_epub: Callable[..., Path]) -> None:
    """Merge a 2-chapter survivor with a 3-chapter and a 1-chapter input."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2"), ("Book B Ch. 3", _L + "b3")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    outcome = merge_epubs(target, [source1, source2])

    assert outcome.contributions == (2, 3, 1)
    assert outcome.chapter_count == 6


def test_contributions_exclude_front_matter(build_epub: Callable[..., Path]) -> None:
    """Merge two inputs where the second carries a title page plus 2 content chapters."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    outcome = merge_epubs(target, [source])

    # Second book's contribution should be 2, not 3 (title page excluded)
    assert outcome.contributions[1] == 2
    assert sum(outcome.contributions) == outcome.chapter_count


def test_contributions_order_matches_input_order(build_epub: Callable[..., Path]) -> None:
    """Merge inputs whose chapter counts are distinct (1, 4, 2)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [
            ("Book B Ch. 1", _L + "b1"),
            ("Book B Ch. 2", _L + "b2"),
            ("Book B Ch. 3", _L + "b3"),
            ("Book B Ch. 4", _L + "b4"),
        ],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1"), ("Book C Ch. 2", _L + "c2")],
        doc_title="Book C",
        filename="c.epub",
    )

    outcome = merge_epubs(target, [source1, source2])

    assert outcome.contributions == (1, 4, 2)


def test_outcome_is_frozen() -> None:
    """Assigning outcome.contributions raises AttributeError or FrozenInstanceError."""
    outcome = MergeOutcome(chapter_count=3, contributions=(1, 2))

    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        outcome.contributions = (1, 1, 1)  # type: ignore[misc]


def test_chapter_map_defaults_empty() -> None:
    """MergeOutcome(chapter_count=3) has chapter_map == ()."""
    outcome = MergeOutcome(chapter_count=3)
    assert outcome.chapter_map == ()


def test_chapter_map_covers_every_merged_chapter(build_epub: Callable[..., Path]) -> None:
    """Merge a 2-chapter survivor with a 1-chapter source; chapter_map has 3 entries.

    Verify that:
    - len(outcome.chapter_map) == outcome.chapter_count == 3
    - input_index sequence is [0, 0, 1]
    - every filename is a basename of a chapter href in the merged book, in order
    """
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    outcome = merge_epubs(target, [source])

    # Verify outcome has the right chapter count
    assert outcome.chapter_count == 3
    assert len(outcome.chapter_map) == 3

    # Verify input_index sequence is [0, 0, 1]
    input_indices = [ch.input_index for ch in outcome.chapter_map]
    assert input_indices == [0, 0, 1]

    # Verify every filename is a basename of a chapter href in the merged book
    doc = EpubDocument.open(target)
    merged_chapters = chapter_table(doc)

    # Extract basenames from chapter hrefs
    chapter_hrefs = [ch.href for ch in merged_chapters]
    chapter_basenames = [Path(href).name for href in chapter_hrefs]

    # Extract filenames from the chapter_map
    filenames = [ch.filename for ch in outcome.chapter_map]

    # Verify filenames match the chapter basenames in order
    assert filenames == chapter_basenames


def test_chapter_map_omits_the_carried_title_page(build_epub: Callable[..., Path]) -> None:
    """Merge with carried title page; chapter_map omits title page entry.

    Survivor with a title page, rewrite_title_page=False:
    - No entry whose filename starts with 'title_page'
    - len(chapter_map) == chapter_count
    """
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
        include_title_page=True,
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    from epub_merge.merge import MergeOptions

    outcome = merge_epubs(target, [source], options=MergeOptions(rewrite_title_page=False))

    # Verify no title page entry in chapter_map
    for ch in outcome.chapter_map:
        assert not ch.filename.startswith("title_page"), (
            f"chapter_map should not contain title page, but got {ch.filename}"
        )

    # Verify chapter_map length matches chapter_count
    assert len(outcome.chapter_map) == outcome.chapter_count


def test_chapter_map_source_hrefs_are_the_inputs_own(build_epub: Callable[..., Path]) -> None:
    """The third entry's source_href equals the source EPUB's chapter href.

    Build EPUBs and verify that source_href for entries from the source
    matches the href as it was in the source EPUB.
    """
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    outcome = merge_epubs(target, [source])

    # The third entry (index 2) should be from the source (input_index=1)
    assert len(outcome.chapter_map) >= 3
    third_entry = outcome.chapter_map[2]
    assert third_entry.input_index == 1

    # Verify that source_href is a path-like string (e.g. "OEBPS/file0001.xhtml")
    assert third_entry.source_href
    assert "/" in third_entry.source_href or "\\" in third_entry.source_href
