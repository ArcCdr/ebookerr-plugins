"""Tests for EPUB stylesheet normalization (TR-NORM-4).

Tests the stylesheet-level rewriter that processes at-rules (@font-face, @media,
@supports), handles rule nesting recursively, and performs orphan cleanup by
dropping rules that end up empty after declaration stripping.
"""

from __future__ import annotations

import dataclasses

from epub_normalize.normalize.counts import NormalizeCounts
from epub_normalize.normalize.options import NormalizeOptions
from epub_normalize.normalize.stylesheet import normalize_stylesheet


def _run(css: str, **kw: bool) -> tuple[str, NormalizeCounts]:
    """Normalize a stylesheet and return the result plus the counts it moved."""
    counts = NormalizeCounts()
    options = dataclasses.replace(NormalizeOptions(), **kw)
    return normalize_stylesheet(css, options=options, counts=counts), counts


class TestFontFaceHandling:
    """Tests for @font-face rule handling."""

    def test_font_face_rule_is_removed(self) -> None:
        """@font-face rules are dropped when font stripping is enabled."""
        css = '@font-face { font-family: "Foo"; src: url(f.ttf); }\np { margin: 0 }'
        result, counts = _run(css)
        assert "@font-face" not in result
        assert "margin" in result
        assert counts.rules_removed == 1

    def test_font_face_kept_when_font_stripping_is_off(self) -> None:
        """@font-face rules are kept when strip_fonts=False."""
        css = '@font-face { font-family: "Foo"; src: url(f.ttf); }\np { margin: 0 }'
        result, counts = _run(css, strip_fonts=False)
        assert "@font-face" in result
        assert counts.rules_removed == 0
        assert result == css


class TestMediaBlockHandling:
    """Tests for @media at-rule handling."""

    def test_media_block_declarations_are_rewritten(self) -> None:
        """Declarations inside @media blocks are rewritten like top-level rules."""
        css = "@media screen { p { color: red; margin: 0 } }"
        result, counts = _run(css)
        assert "@media" in result
        assert "margin" in result
        assert "color" not in result
        assert counts.declarations_removed == 1
        assert counts.rules_removed == 0

    def test_media_block_dropped_when_it_empties(self) -> None:
        """@media block is dropped if all its rules become empty."""
        css = "@media screen { p { color: red } }"
        result, counts = _run(css)
        assert "@media" not in result
        assert counts.rules_removed == 2  # the p rule, then the @media

    def test_nested_media_recursion(self) -> None:
        """@media blocks nested inside @supports are recursively rewritten."""
        css = "@supports (display:flex) { @media screen { p { font-family: X; margin: 0 } } }"
        result, counts = _run(css)
        assert "@supports" in result
        assert "@media" in result
        assert "margin" in result
        assert "font-family" not in result


class TestEmptyRuleCleanup:
    """Tests for orphan rule cleanup."""

    def test_empty_rule_is_dropped(self) -> None:
        """Rules that become empty after stripping are dropped."""
        css = "p { color: red }\nh1 { margin: 0 }"
        result, counts = _run(css)
        assert "p {" not in result
        assert "h1" in result
        assert counts.rules_removed == 1

    def test_preexisting_empty_rule_is_dropped(self) -> None:
        """Empty rules in the input are dropped."""
        css = "h1 { }\np { margin: 0 }"
        result, counts = _run(css)
        assert "h1" not in result
        assert counts.rules_removed == 1


class TestOtherAtRules:
    """Tests for non-nesting at-rules."""

    def test_page_and_import_at_rules_survive(self) -> None:
        """@page, @import, @charset and other at-rules are kept verbatim."""
        css = '@charset "utf-8";\n@import url(other.css);\n@page { margin: 2cm }\np { color: red }'
        result, counts = _run(css)
        assert "@charset" in result
        assert "@import" in result
        assert "@page" in result
        assert "2cm" in result
        assert "color" not in result


class TestFidelityRule:
    """Tests for the fidelity rule: no changes = return input verbatim."""

    def test_no_change_returns_the_input_verbatim(self) -> None:
        """When nothing changes, return the exact input string."""
        css = "<!--\np { margin: 0 }\n-->"
        result, counts = _run(css)
        assert result == css
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0
        assert counts.rules_removed == 0

    def test_second_pass_is_a_no_op(self) -> None:
        """Normalizing an already-normalized stylesheet is a byte-identical no-op."""
        css = "body { font-family: Georgia; color: #222; font-size: 12pt; margin: 20px }"
        result1, counts1 = _run(css)
        result2, counts2 = _run(result1)
        assert result2 == result1
        assert counts2.declarations_removed == 0
        assert counts2.declarations_converted == 0
        assert counts2.rules_removed == 0


class TestPreservedSelectors:
    """Tests for drop-cap and image selectors that are fully preserved."""

    def test_drop_cap_rule_is_untouched(self) -> None:
        """p::first-letter rules are entirely exempt from normalization."""
        css = "p::first-letter { font-family: Georgia; font-size: 300%; color: navy; float: left }"
        result, counts = _run(css)
        assert "Georgia" in result
        assert "navy" in result
        assert "float" in result
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0
        assert counts.rules_removed == 0
        assert result == css


class TestFontSizeHandling:
    """Tests for font-size conversion and removal."""

    def test_font_size_on_body_is_removed_not_converted(self) -> None:
        """font-size on body/html is removed, not converted."""
        css = "body { font-size: 12pt }"
        result, counts = _run(css)
        assert "font-size" not in result
        assert counts.declarations_removed == 1
        assert counts.rules_removed == 1  # the rule emptied

    def test_font_size_on_a_paragraph_is_converted(self) -> None:
        """font-size on other selectors is converted to em."""
        css = "p { font-size: 12pt }"
        result, counts = _run(css)
        assert "1em" in result
        assert counts.declarations_converted == 1
        assert counts.rules_removed == 0


class TestCommentPreservation:
    """Tests for CSS comment and whitespace handling."""

    def test_comments_between_rules_survive(self) -> None:
        """CSS comments are preserved in the output."""
        css = "/* keep me */\np { margin: 0; color: red }"
        result, counts = _run(css)
        assert "/* keep me */" in result


class TestCountAccumulation:
    """Tests for counter accumulation across multiple normalizations."""

    def test_counts_accumulate_across_stylesheets(self) -> None:
        """Running multiple stylesheets through the same counts accumulates totals."""
        css1 = "p { color: red }"
        css2 = "h1 { color: blue }"
        counts = NormalizeCounts()
        options = NormalizeOptions()

        normalize_stylesheet(css1, options=options, counts=counts)
        assert counts.rules_removed == 1

        normalize_stylesheet(css2, options=options, counts=counts)
        assert counts.rules_removed == 2
