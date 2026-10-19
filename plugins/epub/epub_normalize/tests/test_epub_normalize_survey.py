"""Tests for the book survey that fixes a book's body-text size and context (TR-NORM-7)."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import tinycss2
from ebookerr_sdk.epub.document import EpubDocument
from epub_normalize.normalize import NormalizeOptions, options_for_level
from epub_normalize.normalize.survey import FontSize, parse_font_size, survey_book

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _set_css(path: Path, css: str) -> None:
    """Set the content of an EPUB's stylesheet."""
    doc = EpubDocument.open(path)
    doc.write_resource("OEBPS/stylesheet.css", css.encode("utf-8"))
    doc.save()


def _set_chapter(path: Path, body: str) -> None:
    """Replace the only chapter of an EPUB built without a title page with *body*."""
    doc = EpubDocument.open(path)
    chapter = next(
        item for item in doc.opf.manifest() if item.media_type == "application/xhtml+xml"
    )
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
        f"<body>{body}</body></html>"
    )
    doc.write_resource(chapter.href, xhtml.encode("utf-8"))
    doc.save()


def _copy_fixture(tmp_path: Path, name: str) -> Path:
    """Copy a shipped fixture into *tmp_path* and return the copy's path."""
    target = tmp_path / name
    shutil.copy(_FIXTURES / name, target)
    return target


class TestSurveyOfFixtures:
    """The survey of the shipped fixtures."""

    def test_gamma35_survey(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Gamma35 has an 11pt body, root-only classes, and a nav with no blank lines."""
        path = _copy_fixture(tmp_path, "Gamma35.epub")
        caplog.set_level(logging.DEBUG)

        context = survey_book(EpubDocument.open(path), NormalizeOptions(), name="Gamma35.epub")

        assert context.base_size == FontSize(11.0, "pt")
        assert context.root_classes == frozenset({"c9", "doc-content"})
        assert context.root_ids == frozenset()
        assert context.no_blank_lines == frozenset({"nav.xhtml"})
        assert any(
            "Normalize survey Gamma35.epub: base font size 11pt" in record.getMessage()
            for record in caplog.records
        )

    def test_sample_alpha_survey(self, tmp_path: Path) -> None:
        """Sample Alpha has no body-text size, and its title page gets no blank lines."""
        path = _copy_fixture(tmp_path, "sample_alpha.epub")

        context = survey_book(EpubDocument.open(path), NormalizeOptions())

        assert context.base_size is None
        assert "OEBPS/title_page.xhtml" in context.no_blank_lines
        assert "fff_chapter" in context.root_classes


class TestBaseSize:
    """The body-text size is the heaviest size, trusted only at half the block count."""

    def test_a_relative_body_size_is_found(self, build_epub: Callable[..., Path]) -> None:
        """A relative size on the paragraphs and their spans is the body-text size."""
        path = build_epub([("Chapter 1", "https://example.test/1")], include_title_page=False)
        _set_css(path, "p { font-size: 0.917em } .c0 { font-size: 0.917em }")
        _set_chapter(path, '<p><span class="c0">x</span></p>' * 10)

        context = survey_book(EpubDocument.open(path), NormalizeOptions())

        assert context.base_size == FontSize(0.917, "em")

    def test_a_minority_size_is_not_the_base(self, build_epub: Callable[..., Path]) -> None:
        """Two footnotes of 12 blocks are too few to fix a body-text size."""
        path = build_epub([("Chapter 1", "https://example.test/1")], include_title_page=False)
        _set_css(path, ".footnote { font-size: 9pt }")
        _set_chapter(path, "<p>x</p>" * 10 + '<p class="footnote">n</p>' * 2)

        context = survey_book(EpubDocument.open(path), NormalizeOptions())

        assert context.base_size is None


class TestKeptFamilies:
    """Families used by exempt contexts are kept."""

    def test_drop_cap_and_svg_families_are_kept(self, build_epub: Callable[..., Path]) -> None:
        """A drop-cap family and an SVG text family are both kept, lower-cased."""
        path = build_epub([("Chapter 1", "https://example.test/1")], include_title_page=False)
        _set_css(path, 'p::first-letter { font-family: "Goudy Initials" }')
        _set_chapter(
            path,
            '<svg xmlns="http://www.w3.org/2000/svg"><text font-family="Lobster">A</text></svg>',
        )

        context = survey_book(EpubDocument.open(path), NormalizeOptions())

        assert "goudy initials" in context.kept_families
        assert "lobster" in context.kept_families

    def test_a_mathml_family_is_kept(self, build_epub: Callable[..., Path]) -> None:
        """A font family set on MathML content is kept, lower-cased (FR-NORM-3)."""
        path = build_epub([("Chapter 1", "https://example.test/1")], include_title_page=False)
        _set_chapter(
            path,
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            '<mi style="font-family: Stix">x</mi></math>',
        )

        context = survey_book(EpubDocument.open(path), NormalizeOptions())

        assert "stix" in context.kept_families

    def test_strict_keeps_no_drop_cap_family(self, build_epub: Callable[..., Path]) -> None:
        """Strict makes drop caps plain, so their family is not kept."""
        path = build_epub([("Chapter 1", "https://example.test/1")], include_title_page=False)
        _set_css(path, 'p::first-letter { font-family: "Goudy Initials" }')

        context = survey_book(EpubDocument.open(path), options_for_level("strict"))

        assert "goudy initials" not in context.kept_families


class TestFontSizeHelpers:
    """The font-size value model."""

    def test_font_size_helpers(self) -> None:
        """Equal sizes, px conversion and the readable size values."""
        assert FontSize(11, "pt").same_as(FontSize(14.6667, "px")) is True
        assert FontSize(1, "em").same_as(FontSize(100, "%")) is False
        assert FontSize(11, "pt").px == pytest.approx(14.6667, abs=1e-3)
        assert parse_font_size(tinycss2.parse_component_value_list("12pt")) == FontSize(12.0, "pt")
        assert parse_font_size(tinycss2.parse_component_value_list("small")) is None
        assert parse_font_size(tinycss2.parse_component_value_list("calc(1em + 2px)")) is None
        assert parse_font_size(tinycss2.parse_component_value_list("12pt 14pt")) is None
