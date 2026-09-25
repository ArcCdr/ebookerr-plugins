"""Tests for epub_merge.merge.landmarks — EPUB3 landmarks and EPUB2 guide entries."""

from __future__ import annotations

from epub_merge.merge.model import PlannedChapter


def _make_chapter(
    label: str,
    filename: str,
    item_id: str = "default",
    source_href: str = "s.xhtml",
    book_index: int = 0,
    number: int | None = None,
) -> PlannedChapter:
    """Helper to create a PlannedChapter for testing."""
    return PlannedChapter(
        label=label,
        filename=filename,
        item_id=item_id,
        number=number,
        book_index=book_index,
        source_href=source_href,
        xhtml=b"<html></html>",
    )


class TestFirstNarrativeHref:
    """Tests for first_narrative_href logic."""

    def test_first_narrative_skips_title_page(self) -> None:
        """A title page is skipped; the first regular chapter is the narrative start."""
        from epub_merge.merge.landmarks import first_narrative_href

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        result = first_narrative_href(chapters)

        assert result == "c002.xhtml"

    def test_first_narrative_skips_cover_item_id(self) -> None:
        """A chapter with item_id='cover' is skipped (NON_CHAPTER_ITEM_IDS)."""
        from epub_merge.merge.landmarks import first_narrative_href

        chapters = [
            _make_chapter("Cover", "c001.xhtml", item_id="cover"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        result = first_narrative_href(chapters)

        assert result == "c002.xhtml"

    def test_first_narrative_includes_front_matter(self) -> None:
        """Front-matter chapters (prologue, preface) count as narrative content."""
        from epub_merge.merge.landmarks import first_narrative_href

        chapters = [
            _make_chapter("Prologue", "c001.xhtml", item_id="prologue"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        result = first_narrative_href(chapters)

        assert result == "c001.xhtml"

    def test_first_narrative_none_when_only_title_page(self) -> None:
        """When only a title page exists, return None."""
        from epub_merge.merge.landmarks import first_narrative_href

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
        ]

        result = first_narrative_href(chapters)

        assert result is None


class TestBuildLandmarks:
    """Tests for build_landmarks logic."""

    def test_landmarks_include_bodymatter(self) -> None:
        """A bodymatter landmark points to the first narrative chapter."""
        from epub_merge.merge.landmarks import build_landmarks

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        entries = build_landmarks(chapters, cover_href=None)

        bodymatter_entries = [e for e in entries if e.epub_type == "bodymatter"]
        assert len(bodymatter_entries) == 1
        assert bodymatter_entries[0].href == "c002.xhtml"

    def test_landmarks_include_toc_fragment(self) -> None:
        """A toc landmark has href='#toc'."""
        from epub_merge.merge.landmarks import build_landmarks

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        entries = build_landmarks(chapters, cover_href=None)

        toc_entries = [e for e in entries if e.epub_type == "toc"]
        assert len(toc_entries) == 1
        assert toc_entries[0].href == "#toc"

    def test_landmarks_include_cover_when_present(self) -> None:
        """A cover_href value includes a cover landmark at that href; None omits it."""
        from epub_merge.merge.landmarks import build_landmarks

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        entries_with_cover = build_landmarks(chapters, cover_href="cover.xhtml")
        entries_without_cover = build_landmarks(chapters, cover_href=None)

        cover_with = [e for e in entries_with_cover if e.epub_type == "cover"]
        cover_without = [e for e in entries_without_cover if e.epub_type == "cover"]

        assert len(cover_with) == 1
        assert cover_with[0].href == "cover.xhtml"
        assert len(cover_without) == 0

        # The point of taking cover_href rather than a bool: it points at the
        # cover page's actual path, even when that path had to move.
        moved = build_landmarks(chapters, cover_href="cover_1.xhtml")
        assert moved[0].href == "cover_1.xhtml"

    def test_landmarks_include_titlepage_when_present(self) -> None:
        """A titlepage landmark is included when a title-page chapter exists."""
        from epub_merge.merge.landmarks import build_landmarks

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        entries = build_landmarks(chapters, cover_href=None)

        titlepage_entries = [e for e in entries if e.epub_type == "titlepage"]
        assert len(titlepage_entries) == 1
        assert titlepage_entries[0].href == "c001.xhtml"

    def test_landmarks_order(self) -> None:
        """Landmarks appear in the order: cover, titlepage, toc, bodymatter."""
        from epub_merge.merge.landmarks import build_landmarks

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
            _make_chapter("Chapter 1", "c002.xhtml", item_id="chapter1"),
        ]

        entries = build_landmarks(chapters, cover_href="cover.xhtml")

        types = [e.epub_type for e in entries]
        assert types == ["cover", "titlepage", "toc", "bodymatter"]

    def test_landmarks_empty_when_nothing_qualifies(self) -> None:
        """A title page as the only chapter and no cover → only titlepage and toc entries."""
        from epub_merge.merge.landmarks import build_landmarks

        chapters = [
            _make_chapter("Title Page", "c001.xhtml", item_id="title_page"),
        ]

        entries = build_landmarks(chapters, cover_href=None)

        types = [e.epub_type for e in entries]
        assert types == ["titlepage", "toc"]
