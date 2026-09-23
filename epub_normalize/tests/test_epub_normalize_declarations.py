"""Tests for EPUB normalization at the declaration level (FR-NORM-1, FR-NORM-13).

Declaration lists are the leaves of the CSS AST. This module tests the pure function
that filters a declaration list based on property, selector context, and options, and
applies value transformations (absolute-length conversions). These tests drive the
declarative rewriter's implementation.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import tinycss2

from epub_normalize.normalize.counts import NormalizeCounts
from epub_normalize.normalize.declarations import (
    css_text,
    has_absolute_length,
    normalize_declarations,
)
from epub_normalize.normalize.options import NormalizeOptions


def _parse(text: str) -> list[Any]:
    """Parse a declaration list the way the production callers do."""
    return tinycss2.parse_blocks_contents(text, skip_comments=False, skip_whitespace=False)


def _run(text: str, selector: str = "p", **kw: bool) -> tuple[str, NormalizeCounts]:
    """Normalize a declaration list and return its serialized text plus the counts."""
    counts = NormalizeCounts()
    options = dataclasses.replace(NormalizeOptions(), **kw)
    out = normalize_declarations(_parse(text), options=options, selector=selector, counts=counts)
    return css_text(out), counts


class TestFontStripping:
    """Font-related declarations: font-family, font shorthand."""

    def test_font_family_is_stripped(self) -> None:
        """font-family and color are both stripped by default."""
        text, counts = _run("font-family: Georgia, serif; color: red")
        assert "font-family" not in text
        assert "Georgia" not in text
        assert counts.declarations_removed == 2

    def test_font_shorthand_is_stripped(self) -> None:
        """The font shorthand is stripped as a whole."""
        text, counts = _run("font: bold 12pt/1.4 Georgia")
        assert "font" not in text
        assert counts.declarations_removed == 1

    def test_font_stripping_can_be_disabled(self) -> None:
        """When strip_fonts=False, font declarations are preserved."""
        text, counts = _run("font-family: Georgia", strip_fonts=False)
        assert "Georgia" in text
        assert counts.declarations_removed == 0


class TestColorStripping:
    """Color declarations and color shorthands."""

    def test_color_and_background_color_are_stripped(self) -> None:
        """color and background-color are stripped; margin is kept."""
        text, counts = _run("color:#222; background-color:#fff; margin:0")
        assert "color" not in text
        assert "background-color" not in text
        assert "margin" in text
        assert counts.declarations_removed == 2

    def test_background_shorthand_with_a_colour_is_stripped(self) -> None:
        """background shorthand with no url() is stripped."""
        text, counts = _run("background: #fff")
        assert "background" not in text
        assert counts.declarations_removed == 1

    def test_background_shorthand_with_a_url_is_kept(self) -> None:
        """background shorthand with a url() is kept."""
        text, counts = _run('background: #fff url("bg.png") repeat')
        assert "bg.png" in text
        assert counts.declarations_removed == 0

    def test_background_url_check_is_case_insensitive(self) -> None:
        """URL() in any case keeps the background declaration."""
        text, counts = _run("background: URL(bg.png)")
        assert "bg.png" in text
        assert counts.declarations_removed == 0


class TestLineSpacingStripping:
    """Line spacing properties: line-height, letter-spacing, word-spacing."""

    def test_line_spacing_properties_are_stripped(self) -> None:
        """line-height, letter-spacing, word-spacing are all removed."""
        text, counts = _run("line-height:1.4; letter-spacing:2px; word-spacing:1px")
        assert "line-height" not in text
        assert "letter-spacing" not in text
        assert "word-spacing" not in text
        assert counts.declarations_removed == 3


class TestDimensionStripping:
    """Fixed dimension properties: width, height, min-width, min-height, max-height."""

    def test_absolute_dimensions_are_stripped(self) -> None:
        """Absolute dimensions are removed by default."""
        text, counts = _run("width: 600px; height: 800px")
        assert "600px" not in text
        assert "800px" not in text
        assert counts.declarations_removed == 2

    def test_relative_dimensions_are_kept(self) -> None:
        """Percentage widths are preserved."""
        text, counts = _run("width: 90%")
        assert "90%" in text
        assert counts.declarations_removed == 0

    def test_max_width_is_never_stripped(self) -> None:
        """max-width is never stripped; max-height is removed."""
        text, counts = _run("max-width: 100%; max-height: 400px")
        assert "max-width" in text
        assert "max-height" not in text
        assert counts.declarations_removed == 1

    def test_dimensions_kept_on_image_selectors(self) -> None:
        """Dimensions on image/cover/figure selectors are kept."""
        # img selector
        text, counts = _run("width: 600px", selector="img")
        assert "600px" in text
        assert counts.declarations_removed == 0

        # .cover selector
        text, counts = _run("width: 600px", selector=".cover")
        assert "600px" in text
        assert counts.declarations_removed == 0

        # figure selector
        text, counts = _run("width: 600px", selector="figure")
        assert "600px" in text
        assert counts.declarations_removed == 0


class TestAlignmentPreservation:
    """text-align and text-indent: preserved by default, stripped when disabled."""

    def test_alignment_is_preserved_by_default(self) -> None:
        """Alignment properties are kept by default."""
        text, counts = _run("text-align: center; text-indent: 2em")
        assert "text-align" in text
        assert "text-indent" in text
        assert counts.declarations_removed == 0

    def test_alignment_is_stripped_when_disabled(self) -> None:
        """When preserve_alignment=False, both alignment properties are removed."""
        text, counts = _run("text-align: center; text-indent: 2em", preserve_alignment=False)
        assert "text-align" not in text
        assert "text-indent" not in text
        assert counts.declarations_removed == 2


class TestFontSizeConversion:
    """font-size: absolute lengths convert to em; stripped on html/body."""

    def test_font_size_is_converted(self) -> None:
        """Absolute font-size is converted to em."""
        text, counts = _run("font-size: 12pt")
        assert text.strip() == "font-size: 1em;"
        assert counts.declarations_converted == 1
        assert counts.declarations_removed == 0

    def test_font_size_px_is_converted(self) -> None:
        """font-size in px converts to em (24px = 1.5em)."""
        text, counts = _run("font-size: 24px")
        assert "1.5em" in text
        assert counts.declarations_converted == 1

    def test_relative_font_size_is_untouched(self) -> None:
        """Relative font-size (percentages) is not converted."""
        text, counts = _run("font-size: 120%")
        assert "120%" in text
        assert counts.declarations_converted == 0

    def test_font_size_on_body_is_removed(self) -> None:
        """font-size on html/body is removed, not converted."""
        text, counts = _run("font-size: 12pt", selector="body")
        assert text.strip() == ""
        assert counts.declarations_removed == 1
        assert counts.declarations_converted == 0

    def test_font_size_on_html_is_removed(self) -> None:
        """font-size on html selector is removed."""
        text, counts = _run("font-size: 12pt", selector="html")
        assert text.strip() == ""
        assert counts.declarations_removed == 1

    def test_font_size_on_html_body_selector_is_removed(self) -> None:
        """font-size on 'html, body' selector is removed."""
        text, counts = _run("font-size: 12pt", selector="html, body")
        assert text.strip() == ""
        assert counts.declarations_removed == 1

    def test_font_size_conversion_can_be_disabled(self) -> None:
        """When convert_font_sizes=False, font-size is not converted."""
        text, counts = _run("font-size: 12pt", convert_font_sizes=False)
        assert "12pt" in text
        assert counts.declarations_converted == 0


class TestMarginConversion:
    """margin/padding: converted to em only when normalize_margins=True."""

    def test_margins_are_not_converted_by_default(self) -> None:
        """By default, margins are not converted."""
        text, counts = _run("margin: 20px")
        assert "20px" in text
        assert counts.declarations_converted == 0

    def test_margins_are_converted_when_enabled(self) -> None:
        """When normalize_margins=True, margins convert to em."""
        text, counts = _run("margin: 20px", normalize_margins=True)
        assert "1.25em" in text
        assert counts.declarations_converted == 1

    def test_multi_value_margin_converts_every_token(self) -> None:
        """Multi-value margin converts each absolute-length token."""
        text, counts = _run("margin: 16px 8px 0 32px", normalize_margins=True)
        assert "1em" in text
        assert "0.5em" in text
        assert "0" in text
        assert "2em" in text
        assert counts.declarations_converted == 1


class TestPreservedSelectors:
    """Selectors that preserve all styles: drop-cap, first-letter, svg, etc."""

    def test_preserved_selector_short_circuits(self) -> None:
        """Preserved selectors keep all declarations unchanged."""
        input_text = "font-family: Georgia; color: red; font-size: 40pt"

        # Test all preserved selectors
        for selector in ["p::first-letter", ".dropcap", "svg"]:
            text, counts = _run(input_text, selector=selector)
            assert "Georgia" in text
            assert "red" in text
            assert "40pt" in text
            assert counts.declarations_removed == 0
            assert counts.declarations_converted == 0


class TestPreservationOfImportant:
    """The !important flag survives normalization."""

    def test_important_is_preserved_on_surviving_declarations(self) -> None:
        """!important is preserved on declarations that survive."""
        text, counts = _run("margin: 0 !important; color: red")
        assert "!important" in text
        assert "color" not in text


class TestCommentsAndWhitespace:
    """Non-declaration nodes (comments, whitespace) survive normalization."""

    def test_comments_and_whitespace_survive(self) -> None:
        """Comments and whitespace in the declaration list are preserved."""
        text, counts = _run("/* keep */ margin: 0")
        assert "/* keep */" in text


class TestCssTextFunction:
    """css_text serializes a node list to a string."""

    def test_css_text_returns_a_str(self) -> None:
        """css_text returns a str type."""
        result = css_text(_parse("margin:0"))
        assert isinstance(result, str)


class TestHasAbsoluteLength:
    """has_absolute_length detects absolute-length tokens in a value list."""

    def test_has_absolute_length_with_pixels(self) -> None:
        """Pixel values are absolute lengths."""
        declaration = _parse("width: 10px")[0]
        assert has_absolute_length(declaration.value) is True

    def test_has_absolute_length_with_percentage(self) -> None:
        """Percentages are not absolute lengths."""
        declaration = _parse("width: 10%")[0]
        assert has_absolute_length(declaration.value) is False

    def test_has_absolute_length_with_auto(self) -> None:
        """The 'auto' keyword is not an absolute length."""
        declaration = _parse("width: auto")[0]
        assert has_absolute_length(declaration.value) is False


class TestCountAccumulation:
    """Counters accumulate across multiple normalize_declarations calls."""

    def test_counts_accumulate_across_calls(self) -> None:
        """Multiple normalization runs accumulate into the same counts object."""
        counts = NormalizeCounts()
        options = NormalizeOptions()

        # First run
        normalize_declarations(
            _parse("font-family: Georgia; color: red"),
            options=options,
            selector="p",
            counts=counts,
        )
        assert counts.declarations_removed == 2

        # Second run with same counts object
        normalize_declarations(
            _parse("margin: 0 !important; color: blue"),
            options=options,
            selector="p",
            counts=counts,
        )
        assert counts.declarations_removed == 3  # 2 + 1 more (color)
