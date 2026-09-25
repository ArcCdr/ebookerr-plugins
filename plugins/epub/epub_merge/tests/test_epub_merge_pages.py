"""Tests for epub_merge.merge.pages — regenerated title page rendering."""

from __future__ import annotations

import pytest
from ebookerr_sdk.epub.assets import AssetRegistry
from ebookerr_sdk.epub.xhtml import ChapterDocument
from epub_merge.merge.model import PlannedChapter
from epub_merge.merge.pages import (
    XHTML11_DOCTYPE,
    apply_title_page,
    render_title_page,
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


@pytest.fixture
def asset_registry() -> AssetRegistry:
    """Fixture providing a fresh AssetRegistry for each test."""
    return AssetRegistry()


class TestRenderTitlePage:
    """Tests for render_title_page."""

    def test_render_is_well_formed_xml(self) -> None:
        """ChapterDocument.parse(render_title_page(...).decode()) succeeds."""
        result = render_title_page(
            title="Test Book",
            creators=[("Author", None)],
            subjects=[],
            language="en",
            stylesheets=[],
        )
        # Should not raise
        ChapterDocument.parse(result.decode("utf-8"))

    def test_render_has_xhtml11_doctype(self) -> None:
        """The bytes' second line is exactly XHTML11_DOCTYPE, first line is XML declaration."""
        result = render_title_page(
            title="Test Book",
            creators=[],
            subjects=[],
            language="",
            stylesheets=[],
        )
        lines = result.split(b"\n")
        assert lines[0] == b'<?xml version="1.0" encoding="utf-8"?>'
        assert lines[1] == XHTML11_DOCTYPE

    def test_render_shows_title(self) -> None:
        """Title appears in both <title> and <h1>."""
        title_text = "Three Square Meals 174-180"
        result = render_title_page(
            title=title_text,
            creators=[],
            subjects=[],
            language="",
            stylesheets=[],
        )
        result_str = result.decode("utf-8")
        assert f"<title>{title_text}</title>" in result_str
        assert f"<h1>{title_text}</h1>" in result_str

    def test_render_shows_every_author_in_order(self) -> None:
        """Creators are rendered as <p class="author"> in order."""
        creators = [("Tefler", None), ("Ann", "A, A")]
        result = render_title_page(
            title="Test",
            creators=creators,
            subjects=[],
            language="",
            stylesheets=[],
        )
        result_str = result.decode("utf-8")
        # Check both authors appear
        assert '<p class="author">Tefler</p>' in result_str
        assert '<p class="author">Ann</p>' in result_str
        # Check order (Tefler comes before Ann)
        tefler_pos = result_str.find('<p class="author">Tefler</p>')
        ann_pos = result_str.find('<p class="author">Ann</p>')
        assert tefler_pos < ann_pos

    def test_render_shows_subjects_joined(self) -> None:
        """Subjects are rendered as comma-separated in <p class="subjects">."""
        subjects = ("Erotica", "Sci-Fi")
        result = render_title_page(
            title="Test",
            creators=[],
            subjects=subjects,
            language="",
            stylesheets=[],
        )
        result_str = result.decode("utf-8")
        assert '<p class="subjects">Erotica, Sci-Fi</p>' in result_str

    def test_render_omits_empty_sections(self) -> None:
        """No subjects and blank language → no class="subjects" and no class="language"."""
        result = render_title_page(
            title="Test",
            creators=[],
            subjects=[],
            language="",
            stylesheets=[],
        )
        result_str = result.decode("utf-8")
        assert 'class="subjects"' not in result_str
        assert 'class="language"' not in result_str

    def test_render_links_canonical_stylesheets(self) -> None:
        """Stylesheets are linked in <head> and can be read back via stylesheet_hrefs()."""
        stylesheets = ["style.css", "extra.css"]
        result = render_title_page(
            title="Test",
            creators=[],
            subjects=[],
            language="",
            stylesheets=stylesheets,
        )
        doc = ChapterDocument.parse(result.decode("utf-8"))
        assert doc.stylesheet_hrefs() == stylesheets

    def test_render_has_no_inline_styles_or_external_refs(self) -> None:
        """The bytes contain no inline styles or external URLs in content."""
        result = render_title_page(
            title="Test Book",
            creators=[("Author", None), ("Other", "Translator")],
            subjects=["Erotica", "Fantasy"],
            language="en-US",
            stylesheets=["style.css"],
        )
        assert b' style="' not in result
        # Check no external URLs in the body/head (not in DOCTYPE which is required for XHTML 1.1)
        result_str = result.decode("utf-8")
        body_start = result_str.find("<body>")
        head_start = result_str.find("<head>")
        head_end = result_str.find("</head>")
        body_end = result_str.find("</body>")
        # Extract head and body (if head comes before body)
        if head_start >= 0 and head_end >= 0:
            head_section = result_str[head_start:head_end]
            # Only check for https:// in head (not http:// since DOCTYPE is required)
            assert b"https://" not in head_section.encode("utf-8")
        if body_start >= 0 and body_end >= 0:
            body_section = result_str[body_start:body_end]
            assert b"http://" not in body_section.encode("utf-8")
            assert b"https://" not in body_section.encode("utf-8")


class TestApplyTitlePage:
    """Tests for apply_title_page."""

    def test_apply_replaces_existing_title_page(self, asset_registry: AssetRegistry) -> None:
        """When a title_page chapter exists, its xhtml is replaced, other chapters untouched."""
        old_title_page_xhtml = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Old Title</title></head>
<body><h1>Old Title</h1></body>
</html>"""
        chapter_2_xhtml = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><p>Chapter 2 content</p></body>
</html>"""
        title_page = _make_chapter(
            label="Title Page",
            filename="title_page.xhtml",
            item_id="title_page",
            source_href="",
            xhtml=old_title_page_xhtml,
        )
        chapter_2 = _make_chapter(
            label="Chapter 2",
            filename="ch002.xhtml",
            item_id="ch002",
            source_href="ch002.xhtml",
            xhtml=chapter_2_xhtml,
        )
        chapters = [title_page, chapter_2]

        result = apply_title_page(
            chapters,
            title="New Title",
            creators=[],
            subjects=[],
            language="",
            stylesheets=[],
            registry=asset_registry,
        )

        assert len(result) == 2
        # First chapter properties unchanged (filename, item_id, label)
        assert result[0].filename == "title_page.xhtml"
        assert result[0].item_id == "title_page"
        assert result[0].label == "Title Page"
        # First chapter xhtml changed
        assert result[0].xhtml != title_page.xhtml
        assert b"New Title" in result[0].xhtml
        # Second chapter unchanged
        assert result[1].xhtml == chapter_2.xhtml

    def test_apply_matches_by_classification(self, asset_registry: AssetRegistry) -> None:
        """A chapter with label 'Title Page' is recognized and replaced."""
        front_matter_xhtml = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><p>Old</p></body>
</html>"""
        title_page = _make_chapter(
            label="Title Page",
            filename="front_matter.xhtml",
            item_id="front_matter",
            source_href="front_matter.xhtml",
            xhtml=front_matter_xhtml,
        )
        chapters = [title_page]

        result = apply_title_page(
            chapters,
            title="Regenerated Title",
            creators=[],
            subjects=[],
            language="",
            stylesheets=[],
            registry=asset_registry,
        )

        assert len(result) == 1
        assert result[0].xhtml != title_page.xhtml
        assert b"Regenerated Title" in result[0].xhtml

    def test_apply_inserts_when_absent(self, asset_registry: AssetRegistry) -> None:
        """When no title page exists, one is inserted at index 0."""
        chapter_1_xhtml = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><p>Chapter 1</p></body>
</html>"""
        chapter_1 = _make_chapter(
            label="Chapter 1",
            filename="ch001.xhtml",
            item_id="ch001",
            source_href="ch001.xhtml",
            xhtml=chapter_1_xhtml,
        )
        chapters = [chapter_1]

        result = apply_title_page(
            chapters,
            title="New Book",
            creators=[("Author", None)],
            subjects=["Tag"],
            language="en",
            stylesheets=["style.css"],
            registry=asset_registry,
        )

        assert len(result) == 2
        # First chapter is the new title page
        assert result[0].item_id == "title_page"
        assert result[0].filename == "title_page.xhtml"
        assert result[0].label == "Title Page"
        assert result[0].number is None
        assert result[0].book_index == 0
        assert result[0].source_href == ""
        assert b"New Book" in result[0].xhtml
        # Second chapter is the original first chapter
        assert result[1] == chapter_1

    def test_apply_logs_info(
        self, caplog: pytest.LogCaptureFixture, asset_registry: AssetRegistry
    ) -> None:
        """Logs "regenerated" when replacing, "created" when inserting."""
        import logging

        caplog.set_level(logging.INFO, logger="epub_merge.merge.pages")

        # Replace case
        title_page = _make_chapter(
            label="Title Page",
            filename="title_page.xhtml",
            item_id="title_page",
            source_href="",
            xhtml="<html></html>",
        )
        apply_title_page(
            [title_page],
            title="Book Title",
            creators=[],
            subjects=[],
            language="",
            stylesheets=[],
            registry=asset_registry,
        )
        assert "Merge title page regenerated" in caplog.text
        assert "Book Title" in caplog.text

        caplog.clear()

        # Insert case
        chapter = _make_chapter(
            label="Chapter 1",
            filename="ch001.xhtml",
            item_id="ch001",
            source_href="ch001.xhtml",
            xhtml="<html></html>",
        )
        # Create a new registry for the second call to test separate reservations
        asset_registry_2 = AssetRegistry()
        apply_title_page(
            [chapter],
            title="Another Book",
            creators=[],
            subjects=[],
            language="",
            stylesheets=[],
            registry=asset_registry_2,
        )
        assert "Merge title page created" in caplog.text
        assert "Another Book" in caplog.text
