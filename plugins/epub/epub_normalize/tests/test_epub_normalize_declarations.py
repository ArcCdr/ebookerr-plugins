"""Tests for the declaration decision tree (FR-NORM-3..10, FR-NORM-16, FR-NORM-17).

Declaration lists are the leaves of the CSS AST. This module drives the pure function that
filters and converts one declaration list under its rule's context, the level's options and
the book's body-text size.
"""

from __future__ import annotations

from dataclasses import replace

import tinycss2
from epub_normalize.normalize.counts import NormalizeCounts
from epub_normalize.normalize.declarations import normalize_declarations
from epub_normalize.normalize.options import options_for_level
from epub_normalize.normalize.selectors import RuleContext
from epub_normalize.normalize.survey import BookContext, FontSize

ROOT = RuleContext(root=True)
IMAGE = RuleContext(image=True)


def _run(
    decls: str,
    *,
    level: str = "standard",
    context: RuleContext | None = None,
    book: BookContext | None = None,
) -> tuple[dict[str, str], NormalizeCounts]:
    """Normalize *decls* and return the surviving ``name -> value`` map plus the counts."""
    counts = NormalizeCounts()
    parsed = tinycss2.parse_blocks_contents(decls, skip_comments=False, skip_whitespace=False)
    out = normalize_declarations(
        parsed,
        options=options_for_level(level),
        context=RuleContext() if context is None else context,
        book=BookContext() if book is None else book,
        counts=counts,
    )
    survivors = {
        d.lower_name: tinycss2.serialize(d.value).strip() for d in out if d.type == "declaration"
    }
    return survivors, counts


class TestColours:
    """FR-NORM-4: colours are removed at every level, shorthands keep their other parts."""

    def test_colour_longhands_and_shadows_are_removed(self) -> None:
        """Every colour-bearing longhand and shadow goes."""
        out, counts = _run(
            "color: #000; background-color: #fff; -webkit-text-fill-color: #000; "
            "text-shadow: 1px 1px #000; box-shadow: 0 0 2px #333; border-left-color: red"
        )
        assert out == {}
        assert counts.declarations_removed == 6

    def test_border_shorthand_loses_its_colour(self) -> None:
        """A border keeps its width and style."""
        out, counts = _run("border: 1px solid #000")
        assert out == {"border": "1px solid"}
        assert counts.declarations_converted == 1

    def test_background_keeps_its_image_below_the_root(self) -> None:
        """A background drops the colour and keeps the image."""
        out, _ = _run("background: #fff url(paper.png) no-repeat")
        assert out == {"background": "url(paper.png) no-repeat"}

    def test_background_loses_its_image_on_the_root(self) -> None:
        """On the root a background image goes too."""
        out, _ = _run("background: #fff url(paper.png) no-repeat", context=ROOT)
        assert out == {"background": "no-repeat"}

    def test_background_with_only_a_colour_is_removed(self) -> None:
        """Nothing left: the declaration goes."""
        out, counts = _run("background: #ffffff")
        assert out == {}
        assert counts.declarations_removed == 1

    def test_background_gradient_image_is_removed(self) -> None:
        """A gradient background-image goes."""
        out, _ = _run("background-image: linear-gradient(#fff, #eee)")
        assert out == {}

    def test_background_url_image_is_kept_below_the_root(self) -> None:
        """A url() background-image stays outside the root."""
        out, _ = _run("background-image: url(orn.png)")
        assert out == {"background-image": "url(orn.png)"}

    def test_background_url_image_is_removed_on_the_root(self) -> None:
        """A url() background-image goes on the root."""
        out, _ = _run("background-image: url(orn.png)", context=ROOT)
        assert out == {}

    def test_colourless_shorthand_is_untouched(self) -> None:
        """A shorthand with no colour is not rewritten, whitespace included."""
        out, counts = _run("border:1px solid")
        assert out == {"border": "1px solid"}
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0

    def test_colours_survive_when_the_group_is_off(self) -> None:
        """With strip_colors off nothing is removed."""
        counts = NormalizeCounts()
        parsed = tinycss2.parse_blocks_contents("color: red")
        out = normalize_declarations(
            parsed,
            options=replace(options_for_level("standard"), strip_colors=False),
            context=RuleContext(),
            book=BookContext(),
            counts=counts,
        )
        assert [d.lower_name for d in out if d.type == "declaration"] == ["color"]
        assert counts.declarations_removed == 0


class TestFonts:
    """FR-NORM-3: font-family and the font shorthand."""

    def test_font_family_is_removed(self) -> None:
        """A non-monospace family goes."""
        out, _ = _run("font-family: Georgia, serif")
        assert out == {}

    def test_monospace_family_becomes_monospace(self) -> None:
        """A family list naming a monospace family collapses to monospace."""
        out, _ = _run('font-family: "Courier New", monospace')
        assert out == {"font-family": "monospace"}

    def test_plain_monospace_is_kept_untouched(self) -> None:
        """font-family: monospace is already the target and moves no counter."""
        out, counts = _run("font-family: monospace")
        assert out == {"font-family": "monospace"}
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0

    def test_font_shorthand_expands_to_longhands_at_light(self) -> None:
        """The shorthand becomes longhands; the light level keeps size and line height."""
        out, _ = _run("font: italic bold 12pt/1.4 Georgia", level="light")
        assert out == {
            "font-style": "italic",
            "font-variant": "normal",
            "font-weight": "bold",
            "font-size": "12pt",
            "line-height": "1.4",
        }

    def test_font_shorthand_longhands_follow_the_other_rules(self) -> None:
        """At the standard level the size converts and the line height goes."""
        out, _ = _run("font: italic bold 12pt/1.4 Georgia")
        assert out == {
            "font-style": "italic",
            "font-variant": "normal",
            "font-weight": "bold",
            "font-size": "1em",
        }

    def test_unreadable_font_shorthand_is_removed(self) -> None:
        """A system-font keyword cannot be expanded: the declaration goes."""
        out, counts = _run("font: caption")
        assert out == {}
        assert counts.declarations_removed == 1


class TestFontSizes:
    """FR-NORM-7: font sizes."""

    def test_root_size_is_removed(self) -> None:
        """A size on the root goes."""
        out, _ = _run("font-size: 12pt", context=ROOT)
        assert out == {}

    def test_viewport_size_is_removed(self) -> None:
        """A viewport-unit size goes."""
        out, _ = _run("font-size: 5vw")
        assert out == {}

    def test_size_equal_to_the_absolute_body_size_is_removed(self) -> None:
        """The body-text size itself goes."""
        book = BookContext(base_size=FontSize(11.0, "pt"))
        out, _ = _run("font-size: 11pt", book=book)
        assert out == {}

    def test_other_absolute_size_converts_against_the_body_size(self) -> None:
        """Another absolute size becomes em relative to the body-text size."""
        book = BookContext(base_size=FontSize(11.0, "pt"))
        out, _ = _run("font-size: 20pt", book=book)
        assert out == {"font-size": "1.818em"}

    def test_size_equal_to_the_relative_body_size_is_removed(self) -> None:
        """A relative body-text size is compared by unit and value."""
        book = BookContext(base_size=FontSize(0.917, "em"))
        out, _ = _run("font-size: 0.917em", book=book)
        assert out == {}

    def test_other_relative_size_is_kept(self) -> None:
        """A relative size that differs stays."""
        book = BookContext(base_size=FontSize(0.917, "em"))
        out, _ = _run("font-size: 1.667em", book=book)
        assert out == {"font-size": "1.667em"}

    def test_percentage_size_is_kept(self) -> None:
        """A percentage is relative: kept."""
        out, _ = _run("font-size: 90%")
        assert out == {"font-size": "90%"}

    def test_strict_removes_non_heading_sizes(self) -> None:
        """At the strict level only headings keep a size."""
        out, _ = _run("font-size: 1.5em", level="strict")
        assert out == {}

    def test_strict_keeps_a_heading_size(self) -> None:
        """A heading keeps its size at the strict level."""
        out, _ = _run("font-size: 1.5em", level="strict", context=RuleContext(heading=True))
        assert out == {"font-size": "1.5em"}

    def test_light_keeps_absolute_sizes(self) -> None:
        """The light level does not touch sizes."""
        out, _ = _run("font-size: 12pt", level="light")
        assert out == {"font-size": "12pt"}


class TestSpacingHyphenationAlignment:
    """FR-NORM-5, FR-NORM-16 and FR-NORM-9."""

    _SIX = (
        "line-height: 1.15; letter-spacing: 1px; word-spacing: 2px; "
        "adobe-hyphenate: none; -webkit-hyphens: auto; hyphens: manual"
    )

    def test_spacing_and_hyphenation_are_removed(self) -> None:
        """Spacing and hyphenation go at the standard level."""
        out, _ = _run(self._SIX)
        assert out == {}

    def test_light_keeps_spacing_and_hyphenation(self) -> None:
        """The light level keeps all six."""
        out, counts = _run(self._SIX, level="light")
        assert len(out) == 6
        assert counts.declarations_removed == 0

    def test_body_alignments_are_removed(self) -> None:
        """justify and left set the body alignment: removed."""
        assert _run("text-align: justify")[0] == {}
        assert _run("text-align: left")[0] == {}
        assert _run("text-align-last: justify")[0] == {}

    def test_centred_and_right_alignment_is_kept(self) -> None:
        """Centred and right-aligned text keeps its alignment."""
        assert _run("text-align: center")[0] == {"text-align": "center"}
        assert _run("text-align: right")[0] == {"text-align": "right"}

    def test_text_indent_zero_is_kept(self) -> None:
        """text-indent is no longer an alignment property."""
        assert _run("text-indent: 0")[0] == {"text-indent": "0"}


class TestPageLayout:
    """FR-NORM-17: the root's page layout."""

    def test_root_layout_is_removed(self) -> None:
        """Margins, paddings, widths and columns go on the root."""
        out, _ = _run(
            "margin: 2%; padding: 72pt; max-width: 468pt; column-count: 2; width: auto",
            context=ROOT,
        )
        assert out == {}

    def test_non_root_max_width_and_margin_are_kept(self) -> None:
        """Below the root max-width and a relative margin stay."""
        assert _run("max-width: 30em")[0] == {"max-width": "30em"}
        assert _run("margin: 2%")[0] == {"margin": "2%"}


class TestDimensionsAndMargins:
    """FR-NORM-6 and FR-NORM-8."""

    def test_absolute_dimensions_are_removed(self) -> None:
        """Absolute fixed dimensions go."""
        out, _ = _run("width: 600px; height: 11pt; min-height: 20px")
        assert out == {}

    def test_image_context_keeps_dimensions_and_margins(self) -> None:
        """Image contexts keep their dimensions and margins untouched."""
        out, counts = _run("width: 600px; margin: 10px", context=IMAGE)
        assert out == {"width": "600px", "margin": "10px"}
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0

    def test_relative_dimension_is_kept(self) -> None:
        """A relative height stays."""
        assert _run("height: 50%")[0] == {"height": "50%"}

    def test_margin_converts_to_em(self) -> None:
        """An absolute margin becomes em."""
        assert _run("margin: 20px")[0] == {"margin": "1.25em"}

    def test_text_indent_converts_to_em(self) -> None:
        """An absolute indent becomes em."""
        assert _run("text-indent: 24px")[0] == {"text-indent": "1.5em"}

    def test_zero_padding_with_a_unit_becomes_plain_zero(self) -> None:
        """0pt is 0."""
        assert _run("padding-top: 0pt")[0] == {"padding-top": "0"}

    def test_margin_converts_against_the_body_size(self) -> None:
        """The conversion base is the book's body-text size."""
        book = BookContext(base_size=FontSize(11.0, "pt"))
        assert _run("padding-top: 20pt", book=book)[0] == {"padding-top": "1.818em"}

    def test_light_keeps_absolute_margins(self) -> None:
        """The light level does not convert margins."""
        assert _run("margin: 20px", level="light")[0] == {"margin": "20px"}

    def test_important_survives_a_conversion(self) -> None:
        """The !important flag is untouched by a value replacement."""
        counts = NormalizeCounts()
        parsed = tinycss2.parse_blocks_contents("margin: 20px !important")
        out = normalize_declarations(
            parsed,
            options=options_for_level("standard"),
            context=RuleContext(),
            book=BookContext(),
            counts=counts,
        )
        decl = next(d for d in out if d.type == "declaration")
        assert tinycss2.serialize(decl.value).strip() == "1.25em"
        assert decl.important is True


class TestExemptions:
    """FR-NORM-10: vector and drop-cap contexts."""

    def test_vector_context_is_untouched(self) -> None:
        """SVG and MathML contexts keep everything."""
        out, counts = _run(
            "color: #000; font-family: X; width: 600px", context=RuleContext(vector=True)
        )
        assert out == {"color": "#000", "font-family": "X", "width": "600px"}
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0

    _CAP = "font-size: 300%; color: red; float: left; font-family: Goudy; line-height: 1"

    def test_drop_cap_keeps_all_but_colours_below_strict(self) -> None:
        """Below the strict level a drop cap loses only its colours."""
        out, _ = _run(self._CAP, context=RuleContext(drop_cap=True))
        assert out == {
            "font-size": "300%",
            "float": "left",
            "font-family": "Goudy",
            "line-height": "1",
        }

    def test_drop_cap_keeps_nothing_plain_at_strict(self) -> None:
        """At the strict level a drop cap keeps only semantic properties."""
        out, _ = _run(self._CAP, level="strict", context=RuleContext(drop_cap=True))
        assert out == {}

    def test_drop_cap_keeps_weight_at_strict(self) -> None:
        """font-weight is semantic."""
        out, _ = _run(
            "font-weight: bold; float: left", level="strict", context=RuleContext(drop_cap=True)
        )
        assert out == {"font-weight": "bold"}


class TestNeverTouched:
    """Properties no level touches."""

    def test_untouched_properties_are_kept(self) -> None:
        """Structure and semantics stay and move no counter."""
        decls = (
            "white-space: pre-wrap; display: block; page-break-before: always; "
            "font-style: italic; font-weight: bold; text-transform: uppercase; "
            "vertical-align: super; float: left; list-style-type: disc"
        )
        out, counts = _run(decls)
        assert len(out) == 9
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0
