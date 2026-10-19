"""Tests for EPUB normalization rule constants (FR-NORM-3..10, FR-NORM-16, FR-NORM-17)."""

from __future__ import annotations

import pytest
from epub_normalize.normalize import rules
from epub_normalize.normalize.rules import (
    ALIGNMENT_PROPERTIES,
    BODY_ALIGNMENTS,
    BOX_PROPERTIES,
    COLOR_PROPERTIES,
    COLOR_SHORTHAND_PROPERTIES,
    FIXED_SIZE_PROPERTIES,
    FONT_EXTENSIONS,
    FONT_MEDIA_TYPES,
    FONT_SIZE_PROPERTY,
    GRADIENT_FUNCTIONS,
    HYPHENATION_PROPERTIES,
    MONOSPACE_FAMILIES,
    ROOT_LAYOUT_PROPERTIES,
    SEMANTIC_PROPERTIES,
    SPACING_PROPERTIES,
)


class TestPropertySets:
    """Tests for CSS property constant sets."""

    def test_color_sets_split_shorthand_out(self) -> None:
        """COLOR_PROPERTIES and COLOR_SHORTHAND_PROPERTIES are distinct."""
        assert COLOR_PROPERTIES.isdisjoint(COLOR_SHORTHAND_PROPERTIES)
        assert "color" in COLOR_PROPERTIES
        assert "background" in COLOR_SHORTHAND_PROPERTIES

    def test_box_properties_cover_all_sides(self) -> None:
        """BOX_PROPERTIES holds the physical and logical margins, paddings and text-indent."""
        assert len(BOX_PROPERTIES) == 23
        for name in (
            "margin",
            "margin-left",
            "margin-inline-start",
            "margin-block",
            "padding",
            "padding-bottom",
            "padding-inline-end",
            "text-indent",
        ):
            assert name in BOX_PROPERTIES


class TestFontMediaTypesAndExtensions:
    """Tests for FONT_MEDIA_TYPES and FONT_EXTENSIONS."""

    def test_font_media_types_and_extensions(self) -> None:
        """Font media types and extensions include expected MIME types and extensions."""
        assert "font/woff2" in FONT_MEDIA_TYPES
        assert "application/vnd.ms-opentype" in FONT_MEDIA_TYPES
        assert ".ttf" in FONT_EXTENSIONS
        assert ".woff2" in FONT_EXTENSIONS
        assert ".jpg" not in FONT_EXTENSIONS


class TestFontSizeProperty:
    """Tests for FONT_SIZE_PROPERTY constant."""

    def test_font_size_property_value(self) -> None:
        """FONT_SIZE_PROPERTY equals 'font-size'."""
        assert FONT_SIZE_PROPERTY == "font-size"


class TestSpacingProperties:
    """Tests for SPACING_PROPERTIES set."""

    def test_spacing_properties(self) -> None:
        """SPACING_PROPERTIES includes line-height, letter-spacing, word-spacing."""
        assert {
            "line-height",
            "letter-spacing",
            "word-spacing",
        } == SPACING_PROPERTIES


class TestAlignmentProperties:
    """Tests for ALIGNMENT_PROPERTIES set."""

    def test_alignment_properties(self) -> None:
        """ALIGNMENT_PROPERTIES includes text-align and text-align-last, not text-indent."""
        assert {"text-align", "text-align-last"} == ALIGNMENT_PROPERTIES


class TestTwoPointZeroSets:
    """Tests for the 2.0 property sets."""

    def test_color_properties_has_longhands_and_shadows(self) -> None:
        """COLOR_PROPERTIES holds colour longhands and shadows, not geometry."""
        assert "-webkit-text-fill-color" in COLOR_PROPERTIES
        assert "text-shadow" in COLOR_PROPERTIES
        assert "border-left-color" in COLOR_PROPERTIES
        assert "width" not in COLOR_PROPERTIES

    def test_color_shorthand_properties_has_border(self) -> None:
        """COLOR_SHORTHAND_PROPERTIES holds the border shorthand."""
        assert "border" in COLOR_SHORTHAND_PROPERTIES

    def test_hyphenation_properties_has_adobe_hyphenate(self) -> None:
        """HYPHENATION_PROPERTIES holds the Adobe hyphenation property."""
        assert "adobe-hyphenate" in HYPHENATION_PROPERTIES

    def test_body_alignments_excludes_center(self) -> None:
        """BODY_ALIGNMENTS holds body-text alignments and not centring."""
        assert "justify" in BODY_ALIGNMENTS
        assert "center" not in BODY_ALIGNMENTS

    def test_fixed_size_properties_excludes_max_width(self) -> None:
        """FIXED_SIZE_PROPERTIES does not remove max-width outside the root."""
        assert "max-width" not in FIXED_SIZE_PROPERTIES

    def test_root_layout_properties_includes_max_width(self) -> None:
        """ROOT_LAYOUT_PROPERTIES removes max-width on the root."""
        assert "max-width" in ROOT_LAYOUT_PROPERTIES

    def test_semantic_properties_keeps_font_style_not_size(self) -> None:
        """SEMANTIC_PROPERTIES keeps font-style and never font-size."""
        assert "font-style" in SEMANTIC_PROPERTIES
        assert "font-size" not in SEMANTIC_PROPERTIES

    def test_monospace_families_has_courier_new(self) -> None:
        """MONOSPACE_FAMILIES names Courier New in lower case."""
        assert "courier new" in MONOSPACE_FAMILIES

    def test_gradient_functions_has_linear_gradient(self) -> None:
        """GRADIENT_FUNCTIONS names linear-gradient."""
        assert "linear-gradient" in GRADIENT_FUNCTIONS

    @pytest.mark.parametrize(
        "name",
        [
            "PRESERVE_SELECTOR_RE",
            "IMAGE_SELECTOR_RE",
            "ROOT_SELECTOR_RE",
            "is_preserved_selector",
            "is_image_selector",
            "is_root_selector",
            "FONT_PROPERTIES",
            "DIMENSION_PROPERTIES",
            "NEVER_STRIPPED_PROPERTIES",
            "COLOR_PROPERTIES_2",
            "COLOR_SHORTHAND_PROPERTIES_2",
            "ALIGNMENT_PROPERTIES_2",
            "BOX_PROPERTIES_2",
        ],
    )
    def test_the_regex_predicates_are_gone(self, name: str) -> None:
        """The 1.x regex predicates and sets, and the _2 suffixed names, no longer exist."""
        assert hasattr(rules, name) is False
