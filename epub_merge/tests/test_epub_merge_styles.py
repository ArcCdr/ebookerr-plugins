"""Tests for epub_merge.merge.styles — canonical stylesheet application."""

from __future__ import annotations

import pytest
from epub_merge.merge.assets import AssetPlan
from epub_merge.merge.model import InputBook, PlannedChapter
from epub_merge.merge.styles import (
    apply_canonical_stylesheets,
    canonical_stylesheets,
)


def _make_chapter(
    label: str,
    filename: str,
    item_id: str,
    source_href: str,
    xhtml: str,
    book_index: int = 0,
    number: int | None = None,
) -> PlannedChapter:
    """Helper to create a PlannedChapter from XHTML string."""
    return PlannedChapter(
        label=label,
        filename=filename,
        item_id=item_id,
        number=number,
        book_index=book_index,
        source_href=source_href,
        xhtml=xhtml.encode("utf-8"),
    )


def _make_input_book(
    index: int,
    stylesheets: tuple[str, ...] = (),
    name: str = "test.epub",
) -> InputBook:
    """Helper to create a minimal InputBook."""
    return InputBook(
        index=index,
        name=name,
        version="2.0",
        title=f"Book {index}",
        creators=(),
        contributors=(),
        language="en",
        identifier=f"uid:{index}",
        source=None,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root="OEBPS",
        chapters=(),
        resources=(),
        stylesheets=stylesheets,
        cover_href=None,
        cover_media_type=None,
    )


class TestCanonicalStylesheets:
    """Test canonical_stylesheets function."""

    def test_canonical_maps_survivor_stylesheets(self) -> None:
        """Survivor stylesheets=("OEBPS/style.css",) mapped to "style.css" returns ["style.css"]."""
        survivor = _make_input_book(0, stylesheets=("OEBPS/style.css",))
        books = [survivor]
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={(0, "OEBPS/style.css"): "style.css"},
        )

        result = canonical_stylesheets(books, assets)

        assert result == ["style.css"]

    def test_canonical_preserves_order(self) -> None:
        """Survivor with two stylesheets returns both in the survivor's order."""
        survivor = _make_input_book(0, stylesheets=("OEBPS/style1.css", "OEBPS/style2.css"))
        books = [survivor]
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={
                (0, "OEBPS/style1.css"): "style1.css",
                (0, "OEBPS/style2.css"): "style2.css",
            },
        )

        result = canonical_stylesheets(books, assets)

        assert result == ["style1.css", "style2.css"]

    def test_canonical_empty_when_survivor_has_none(self) -> None:
        """Survivor with no stylesheets returns []."""
        survivor = _make_input_book(0, stylesheets=())
        books = [survivor]
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={},
        )

        result = canonical_stylesheets(books, assets)

        assert result == []

    def test_canonical_skips_unmapped(self) -> None:
        """A survivor stylesheet with no mapping entry is skipped."""
        survivor = _make_input_book(0, stylesheets=("OEBPS/style1.css", "OEBPS/style2.css"))
        books = [survivor]
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={(0, "OEBPS/style1.css"): "style1.css"},
            # Note: (0, "OEBPS/style2.css") is not in mapping
        )

        result = canonical_stylesheets(books, assets)

        assert result == ["style1.css"]


class TestApplyCanonicalStylesheets:
    """Test apply_canonical_stylesheets function."""

    def test_survivor_chapters_untouched(self) -> None:
        """A book_index == 0 chapter keeps its link and exact bytes."""
        xhtml = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><link rel="stylesheet" href="other.css"/></head><body/></html>'
        )
        chapter = _make_chapter(
            label="Survivor Ch 1",
            filename="chapter_001.xhtml",
            item_id="chapter_001",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
            book_index=0,
        )

        result = apply_canonical_stylesheets([chapter], ["style.css"])

        assert len(result) == 1
        assert result[0].xhtml == chapter.xhtml

    def test_non_survivor_chapter_relinked(self) -> None:
        """A book_index == 1 chapter with stylesheet link gets relinked to canonical."""
        xhtml = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><link rel="stylesheet" href="whatever.css"/></head><body/></html>'
        )
        chapter = _make_chapter(
            label="Non-Survivor Ch 1",
            filename="chapter_002.xhtml",
            item_id="chapter_002",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
            book_index=1,
        )

        result = apply_canonical_stylesheets([chapter], ["style.css"])

        assert len(result) == 1
        xml = result[0].xhtml.decode("utf-8")
        assert 'href="style.css"' in xml
        assert 'href="whatever.css"' not in xml

    def test_non_survivor_unstyled_chapter_gets_no_link(self) -> None:
        """A book_index == 1 chapter with no stylesheet link stays unchanged."""
        xhtml = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head/><body/></html>'
        )
        chapter = _make_chapter(
            label="Unstyled Ch 1",
            filename="chapter_003.xhtml",
            item_id="chapter_003",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
            book_index=1,
        )

        result = apply_canonical_stylesheets([chapter], ["style.css"])

        assert len(result) == 1
        assert result[0].xhtml == chapter.xhtml
        assert b"<link" not in result[0].xhtml

    def test_multiple_canonical_stylesheets_all_linked(self) -> None:
        """Relinked chapter with canonical == ["a.css", "b.css"] contains both, in order."""
        xhtml = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><link rel="stylesheet" href="old.css"/></head><body/></html>'
        )
        chapter = _make_chapter(
            label="Multi-Style Ch",
            filename="chapter_004.xhtml",
            item_id="chapter_004",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
            book_index=1,
        )

        result = apply_canonical_stylesheets([chapter], ["a.css", "b.css"])

        assert len(result) == 1
        xml = result[0].xhtml.decode("utf-8")
        # Both hrefs should be present
        assert 'href="a.css"' in xml
        assert 'href="b.css"' in xml
        # Old href should not be present
        assert 'href="old.css"' not in xml

    def test_empty_canonical_removes_links(self) -> None:
        """Styled non-survivor chapter with canonical == [] has stylesheet link removed."""
        xhtml = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><link rel="stylesheet" href="old.css"/></head><body/></html>'
        )
        chapter = _make_chapter(
            label="Removed-Style Ch",
            filename="chapter_005.xhtml",
            item_id="chapter_005",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
            book_index=1,
        )

        result = apply_canonical_stylesheets([chapter], [])

        assert len(result) == 1
        xml = result[0].xhtml.decode("utf-8")
        assert 'rel="stylesheet"' not in xml
        assert 'href="old.css"' not in xml

    def test_malformed_chapter_is_kept_and_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        """Unparseable XHTML is kept unchanged and logged at WARNING."""
        xhtml = "<this is not valid xml>"
        chapter = _make_chapter(
            label="Malformed Ch",
            filename="chapter_006.xhtml",
            item_id="chapter_006",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
            book_index=1,
        )

        with caplog.at_level("WARNING"):
            result = apply_canonical_stylesheets([chapter], ["style.css"])

        assert len(result) == 1
        # Bytes should be unchanged
        assert result[0].xhtml == chapter.xhtml
        # Should have logged a warning
        assert "could not relink stylesheet" in caplog.text.lower()

    def test_logs_info_summary(self, caplog: pytest.LogCaptureFixture) -> None:
        """Call logs an info message with summary."""
        xhtml = (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><link rel="stylesheet" href="old.css"/></head><body/></html>'
        )
        chapter1 = _make_chapter(
            label="Ch1",
            filename="chapter_001.xhtml",
            item_id="chapter_001",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
            book_index=1,
        )
        chapter2 = _make_chapter(
            label="Ch2",
            filename="chapter_002.xhtml",
            item_id="chapter_002",
            source_href="OEBPS/ch2.xhtml",
            xhtml=xhtml,
            book_index=1,
        )

        with caplog.at_level("INFO"):
            result = apply_canonical_stylesheets([chapter1, chapter2], ["style.css"])

        assert len(result) == 2
        # Should have logged a summary
        assert "Merge relinked" in caplog.text
        assert "2" in caplog.text or "non-survivor" in caplog.text.lower()
