"""Tests for EPUB stylesheet normalization (TR-NORM-4).

Tests the stylesheet-level rewriter that processes at-rules (@font-face, @media,
@supports), handles rule nesting recursively, and performs orphan cleanup by
dropping rules that end up empty after declaration stripping.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest
import tinycss2
from epub_normalize.normalize.counts import NormalizeCounts
from epub_normalize.normalize.options import NormalizeOptions, options_for_level
from epub_normalize.normalize.stylesheet import normalize_stylesheet
from epub_normalize.normalize.survey import BookContext


def _run(css: str, **kw: bool) -> tuple[str, NormalizeCounts]:
    """Normalize a stylesheet and return the result plus the counts it moved."""
    counts = NormalizeCounts()
    options = dataclasses.replace(NormalizeOptions(), **kw)
    return normalize_stylesheet(css, options=options, counts=counts), counts


def _normalize(
    css: str, *, level: str = "standard", book: BookContext | None = None
) -> tuple[str, NormalizeCounts]:
    """Normalize a stylesheet at one styling level against a book (empty when omitted)."""
    counts = NormalizeCounts()
    result = normalize_stylesheet(
        css,
        options=options_for_level(level),
        counts=counts,
        book=BookContext() if book is None else book,
    )
    return result, counts


def declarations_of(css: str) -> dict[str, dict[str, str]]:
    """Map each top-level rule's selector text to its {property: value}, whitespace-stripped."""
    out: dict[str, dict[str, str]] = {}
    for rule in tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True):
        if rule.type != "qualified-rule":
            continue
        decls = tinycss2.parse_blocks_contents(
            rule.content, skip_comments=True, skip_whitespace=True
        )
        out[tinycss2.serialize(rule.prelude).strip()] = {
            d.lower_name: tinycss2.serialize(d.value).strip()
            for d in decls
            if d.type == "declaration"
        }
    return out


def _page_declarations(css: str) -> list[str]:
    """The declaration names inside the stylesheet's first @page block."""
    for node in tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True):
        if node.type == "at-rule" and node.lower_at_keyword == "page":
            body = tinycss2.parse_blocks_contents(
                node.content, skip_comments=True, skip_whitespace=True
            )
            return [d.lower_name for d in body if d.type == "declaration"]
    pytest.fail("no @page block in the output")


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
        """@import and @charset are kept verbatim; a @page whose margins go is dropped."""
        css = '@charset "utf-8";\n@import url(other.css);\n@page { margin: 2cm }\np { color: red }'
        result, counts = _run(css)
        assert "@charset" in result
        assert "@import" in result
        assert "@page" not in result
        assert "2cm" not in result
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
    """Tests for drop-cap selectors, which keep everything but their colours."""

    def test_drop_cap_rule_keeps_everything_but_colours(self) -> None:
        """p::first-letter keeps family, size and float; its colour goes (FR-NORM-10)."""
        css = "p::first-letter { font-family: Georgia; font-size: 300%; color: navy; float: left }"
        result, counts = _run(css)
        assert "Georgia" in result
        assert "300%" in result
        assert "navy" not in result
        assert "float" in result
        assert counts.declarations_removed == 1
        assert counts.declarations_converted == 0
        assert counts.rules_removed == 0


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


class TestSelectorContextSplit:
    """Tests for splitting a selector list into one rule per selector context (TR-NORM-6)."""

    def test_a_mixed_selector_list_is_split(self, caplog: pytest.LogCaptureFixture) -> None:
        """A list mixing a heading and a drop cap splits; only the drop cap's font survives."""
        caplog.set_level(logging.DEBUG)
        css = "h1, p.dropcap { color: #000000; font-family: Goudy; }"
        result, _counts = _normalize(css)
        assert declarations_of(result) == {"p.dropcap": {"font-family": "Goudy"}}
        assert "Normalize split a rule into 2 by selector context: h1, p.dropcap" in caplog.messages

    def test_a_mixed_selector_list_at_strict_is_removed(self) -> None:
        """At the strict level neither group keeps a declaration, so the whole rule goes."""
        result, _counts = _normalize(
            "h1, p.dropcap { color: #000000; font-family: Goudy; }", level="strict"
        )
        assert declarations_of(result) == {}

    def test_one_context_is_not_split(self) -> None:
        """A list whose selectors share one context is rewritten as one rule."""
        result, counts = _normalize("h1, h2 { color: red; }")
        assert declarations_of(result) == {}
        assert counts.rules_removed == 1
        unchanged = "h1, h2 { font-weight: bold; }"
        assert _normalize(unchanged)[0] == unchanged

    def test_an_unchanged_mixed_list_is_left_as_is(self) -> None:
        """A mixed list with nothing to change is kept exactly, with no split and no counts."""
        css = "h1, p.dropcap { font-weight: bold; }"
        result, counts = _normalize(css)
        assert result == css
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0
        assert counts.rules_removed == 0

    def test_an_svg_selector_keeps_its_colour(self) -> None:
        """Colour on an SVG text selector survives while the same colour on a paragraph goes."""
        result, _counts = _normalize("p, svg text { color: red; }")
        assert declarations_of(result) == {"svg text": {"color": "red"}}

    def test_a_root_selector_in_a_list_does_not_strip_paragraph_margins(self) -> None:
        """A root selector ahead of a paragraph does not take the paragraph's margins."""
        result, _counts = _normalize("body, p { margin: 0; font-size: 12pt }")
        assert declarations_of(result) == {"p": {"margin": "0", "font-size": "1em"}}


class TestFontFaceKeptFamilies:
    """Tests for @font-face rules whose family a drop cap or vector content uses (FR-NORM-3)."""

    FONT_FACE = '@font-face { font-family: "Goudy Initials"; src: url(g.otf) }'
    DROP_CAP = 'p::first-letter { font-family: "Goudy Initials" }'

    def test_font_face_kept_for_a_drop_cap_family(self) -> None:
        """An @font-face whose family the book uses is kept verbatim."""
        book = BookContext(kept_families=frozenset({"goudy initials"}))
        result, _counts = _normalize(f"{self.FONT_FACE} {self.DROP_CAP}", book=book)
        assert self.FONT_FACE in result

    def test_font_face_dropped_without_a_kept_family(self) -> None:
        """An @font-face with no kept family is removed."""
        result, _counts = _normalize(f"{self.FONT_FACE} {self.DROP_CAP}")
        assert "@font-face" not in result


class TestPageMargins:
    """Tests for the @page margins removed at the standard and strict levels (FR-NORM-17)."""

    def test_page_margins_are_removed(self) -> None:
        """Margin descriptors leave the @page block; other descriptors stay."""
        result, _counts = _normalize("@page { margin: 1in; size: a5 }")
        assert "margin" not in result
        assert _page_declarations(result) == ["size"]

    def test_a_page_with_only_margins_is_removed(self) -> None:
        """An @page left with no declaration is dropped."""
        result, _counts = _normalize("@page { margin: 5pt }")
        assert "@page" not in result

    def test_page_margins_are_kept_at_the_light_level(self) -> None:
        """At the light level the @page is not touched at all."""
        css = "@page { margin: 1in; size: a5 }"
        assert _normalize(css, level="light")[0] == css


class TestEmptyMediaBlock:
    """Tests for @media blocks that empty out (TR-NORM-4)."""

    def test_a_media_block_that_empties_is_removed(self) -> None:
        """A @media block whose rules all lose their declarations is dropped."""
        result, _counts = _normalize("@media screen { body { margin: 2% } p { color: red } }")
        assert "@media" not in result
