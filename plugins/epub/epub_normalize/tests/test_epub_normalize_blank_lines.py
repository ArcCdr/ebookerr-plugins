"""Tests for the blank-line marking of empty paragraphs (FR-NORM-18)."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from ebookerr_sdk.epub.document import EpubDocument
from epub_normalize.normalize import NormalizeOptions, normalize_epub
from epub_normalize.normalize.counts import NormalizeCounts
from epub_normalize.normalize.options import options_for_level
from epub_normalize.normalize.xhtml import mark_blank_lines, normalize_xhtml

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_GAMMA35 = _FIXTURES / "Gamma35.epub"
_ALPHA = _FIXTURES / "sample_alpha.epub"


@pytest.mark.parametrize(
    ("text", "expected", "count"),
    [
        (
            '<p class="c0">\n  <span class="c1"/>\n</p>',
            '<p class="c0">\n  <span class="c1"/>\n&#160;</p>',
            1,
        ),
        ("<p/>", "<p>&#160;</p>", 1),
        ('<p class="x" />', '<p class="x">&#160;</p>', 1),
        ("<p></p>", "<p>&#160;</p>", 1),
        ("<p> </p>", "<p> &#160;</p>", 1),
        ("<p>&#160;</p>", "<p>&#160;</p>", 0),
        ("<p><br/></p>", "<p><br/></p>", 0),
        ("<p>\x000\x00</p>", "<p>\x000\x00</p>", 0),
        ("<pre></pre>", "<pre></pre>", 0),
        (
            '<p class="c2"><span class="c1">Text</span></p>',
            '<p class="c2"><span class="c1">Text</span></p>',
            0,
        ),
    ],
)
def test_mark_blank_lines_cases(text: str, expected: str, count: int) -> None:
    """Each measured empty-paragraph shape is marked (or left) as the contract states."""
    assert mark_blank_lines(text) == (expected, count)


@pytest.mark.parametrize("level", ["light", "standard", "strict"])
def test_a_chapter_keeps_its_blank_line_at_every_level(level: str) -> None:
    """An empty paragraph gets one &#160; at every styling level."""
    counts = NormalizeCounts()
    result = normalize_xhtml(
        "<html><body><p>a</p><p></p><p>b</p></body></html>",
        options=options_for_level(level),
        counts=counts,
        name="c.xhtml",
        blank_lines=True,
    )
    assert "<p>&#160;</p>" in result
    assert counts.blank_lines_marked == 1


def test_blank_lines_false_leaves_empty_paragraphs() -> None:
    """With blank_lines off, the chapter keeps its empty paragraph byte for byte."""
    text = "<html><body><p>a</p><p></p><p>b</p></body></html>"
    counts = NormalizeCounts()
    result = normalize_xhtml(
        text, options=NormalizeOptions(), counts=counts, name="c.xhtml", blank_lines=False
    )
    assert result == text


def test_an_empty_paragraph_inside_svg_is_untouched() -> None:
    """An empty paragraph inside an inline SVG subtree is never marked."""
    text = "<svg><foreignObject><p></p></foreignObject></svg>"
    counts = NormalizeCounts()
    result = normalize_xhtml(
        text, options=NormalizeOptions(), counts=counts, name="c.xhtml", blank_lines=True
    )
    assert result == text


def test_a_second_pass_marks_nothing() -> None:
    """Running the chapter rewrite on its own output marks no further paragraph."""
    text = "<html><body><p>a</p><p></p><p>b</p></body></html>"
    first = normalize_xhtml(
        text, options=NormalizeOptions(), counts=NormalizeCounts(), name="c.xhtml", blank_lines=True
    )
    counts = NormalizeCounts()
    second = normalize_xhtml(
        first, options=NormalizeOptions(), counts=counts, name="c.xhtml", blank_lines=True
    )
    assert counts.blank_lines_marked == 0
    assert second == first


def test_gamma35_blank_lines_survive(tmp_path: Path) -> None:
    """Every one of the 1 401 blank lines of Gamma35 still shows after Normalize."""
    p = tmp_path / "gamma.epub"
    shutil.copy(_GAMMA35, p)
    normalize_epub(p, options=NormalizeOptions())
    with zipfile.ZipFile(p) as archive:
        text = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith((".xhtml", ".html"))
        )
    assert text.count("&#160;</p>") == 1401


def test_the_title_page_summary_paragraph_is_left_alone(tmp_path: Path) -> None:
    """The title page keeps its empty paragraph, and the book keeps its description."""
    p = tmp_path / "alpha.epub"
    shutil.copy(_ALPHA, p)
    description = EpubDocument.open(p).read_metadata().description
    normalize_epub(p, options=NormalizeOptions())
    with zipfile.ZipFile(p) as archive:
        title_page = archive.read("OEBPS/title_page.xhtml").decode("utf-8")
    assert "<b>Summary:</b> <p></p>" in title_page
    assert EpubDocument.open(p).read_metadata().description == description
