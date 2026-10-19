"""Tests for the EPUB normalizer's value helpers (FR-NORM-3, FR-NORM-4)."""

from __future__ import annotations

from typing import Any

import tinycss2
from epub_normalize.normalize.values import (
    convert_lengths,
    expand_font_shorthand,
    family_names,
    has_absolute_length,
    is_monospace,
    is_viewport_length,
    strip_colors,
)


def _v(text: str) -> list[Any]:
    """Parse a component-value list the way the declaration rewriter does."""
    return tinycss2.parse_component_value_list(text)


class TestExpandFontShorthand:
    """expand_font_shorthand reads the longhands a font shorthand sets."""

    def test_full_shorthand_with_monospace_family(self) -> None:
        """A monospace family adds the font-family longhand."""
        assert expand_font_shorthand(_v('italic bold 12pt/1.4 "Courier New", monospace')) == [
            ("font-style", "italic"),
            ("font-variant", "normal"),
            ("font-weight", "bold"),
            ("font-size", "12pt"),
            ("line-height", "1.4"),
            ("font-family", "monospace"),
        ]

    def test_size_and_family_only(self) -> None:
        """Without a weight or style the keywords default to normal."""
        assert expand_font_shorthand(_v("12pt Georgia")) == [
            ("font-style", "normal"),
            ("font-variant", "normal"),
            ("font-weight", "normal"),
            ("font-size", "12pt"),
            ("line-height", "normal"),
        ]

    def test_small_caps_numeric_weight_and_percentage_size(self) -> None:
        """A numeric weight and a percentage size are read as written."""
        assert expand_font_shorthand(_v("small-caps 700 90% Arial")) == [
            ("font-style", "normal"),
            ("font-variant", "small-caps"),
            ("font-weight", "700"),
            ("font-size", "90%"),
            ("line-height", "normal"),
        ]

    def test_system_font_keyword_is_unreadable(self) -> None:
        """A system font keyword cannot be expanded."""
        assert expand_font_shorthand(_v("caption")) is None

    def test_var_function_is_unreadable(self) -> None:
        """A var() value cannot be expanded."""
        assert expand_font_shorthand(_v("var(--x)")) is None

    def test_no_size_is_unreadable(self) -> None:
        """A shorthand without a size is not a valid font value."""
        assert expand_font_shorthand(_v("bold")) is None


class TestStripColors:
    """strip_colors removes colour components and gradients from a shorthand."""

    def test_border_keeps_width_and_style(self) -> None:
        """The colour is dropped and the rest keeps its order."""
        result = strip_colors(_v("1px solid #000"), drop_images=False)
        assert tinycss2.serialize(result) == " 1px solid"

    def test_background_image_kept_without_drop_images(self) -> None:
        """A url() image stays when images are not dropped."""
        result = strip_colors(_v("#fff url(paper.png) no-repeat"), drop_images=False)
        assert tinycss2.serialize(result) == " url(paper.png) no-repeat"

    def test_background_image_dropped_with_drop_images(self) -> None:
        """A url() image is removed when images are dropped."""
        result = strip_colors(_v("#fff url(paper.png) no-repeat"), drop_images=True)
        assert tinycss2.serialize(result) == " no-repeat"

    def test_only_a_colour_leaves_nothing(self) -> None:
        """A value that is only a colour leaves nothing to keep."""
        assert strip_colors(_v("#ffffff"), drop_images=False) is None

    def test_keyword_kept(self) -> None:
        """A non-colour keyword is kept."""
        result = strip_colors(_v("underline red"), drop_images=False)
        assert tinycss2.serialize(result) == " underline"

    def test_none_kept(self) -> None:
        """The none keyword is not a colour and is kept."""
        result = strip_colors(_v("none"), drop_images=False)
        assert tinycss2.serialize(result) == " none"

    def test_gradient_only_leaves_nothing(self) -> None:
        """A gradient is a colour component and is dropped."""
        assert strip_colors(_v("linear-gradient(#fff, #eee)"), drop_images=False) is None

    def test_currentcolor_is_a_colour(self) -> None:
        """currentColor is a colour and is dropped."""
        result = strip_colors(_v("2px dotted currentColor"), drop_images=False)
        assert tinycss2.serialize(result) == " 2px dotted"


class TestFamilyNames:
    """family_names reads the lower-case family names of a font-family value."""

    def test_quoted_and_generic_family(self) -> None:
        """A quoted name keeps its spaces and is lower-cased."""
        assert family_names(_v('"Courier New", monospace')) == ["courier new", "monospace"]

    def test_multi_word_ident_family(self) -> None:
        """An unquoted multi-word name is joined by single spaces."""
        assert family_names(_v("Times New Roman, serif")) == ["times new roman", "serif"]


class TestIsMonospace:
    """is_monospace checks the family list against the monospace families."""

    def test_monospace_family_detected(self) -> None:
        """A listed monospace family is detected."""
        assert is_monospace(_v("Consolas, serif")) is True

    def test_serif_family_not_monospace(self) -> None:
        """A serif family is not monospace."""
        assert is_monospace(_v("Georgia, serif")) is False


class TestViewportLength:
    """is_viewport_length detects viewport-relative dimensions."""

    def test_vw_is_viewport(self) -> None:
        """A vw dimension is a viewport length."""
        assert is_viewport_length(_v("5vw")) is True

    def test_em_is_not_viewport(self) -> None:
        """An em dimension is not a viewport length."""
        assert is_viewport_length(_v("1.2em")) is False


class TestAbsoluteLength:
    """has_absolute_length detects absolute-length tokens in a value list."""

    def test_pixels_are_absolute(self) -> None:
        """Pixel values are absolute lengths."""
        assert has_absolute_length(_v("10px")) is True

    def test_percentage_is_not_absolute(self) -> None:
        """Percentages are not absolute lengths."""
        assert has_absolute_length(_v("10%")) is False

    def test_points_are_absolute(self) -> None:
        """Point values are absolute lengths."""
        assert has_absolute_length(_v("12pt")) is True

    def test_auto_is_not_absolute(self) -> None:
        """The 'auto' keyword is not an absolute length."""
        assert has_absolute_length(_v("auto")) is False


class TestConvertLengths:
    """convert_lengths replaces absolute lengths with em against a base."""

    def test_px_converted_against_default_base(self) -> None:
        """20 px on a 16 px base serialises to 1.25em and reports a change."""
        value, changed = convert_lengths(_v("20px"), base_px=16.0)
        assert tinycss2.serialize(value) == "1.25em"
        assert changed is True
