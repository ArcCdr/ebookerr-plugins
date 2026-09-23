"""Tests for EPUB normalization rule constants and selector predicates."""

from __future__ import annotations

import re

from epub_normalize.normalize.rules import (
    ALIGNMENT_PROPERTIES,
    BOX_PROPERTIES,
    COLOR_PROPERTIES,
    COLOR_SHORTHAND_PROPERTIES,
    DIMENSION_PROPERTIES,
    FONT_EXTENSIONS,
    FONT_MEDIA_TYPES,
    FONT_PROPERTIES,
    FONT_SIZE_PROPERTY,
    IMAGE_SELECTOR_RE,
    NEVER_STRIPPED_PROPERTIES,
    PRESERVE_SELECTOR_RE,
    ROOT_SELECTOR_RE,
    SPACING_PROPERTIES,
    is_image_selector,
    is_preserved_selector,
    is_root_selector,
)


class TestPropertySets:
    """Tests for CSS property constant sets."""

    def test_max_width_is_never_stripped(self) -> None:
        """max-width is in NEVER_STRIPPED_PROPERTIES but not in DIMENSION_PROPERTIES."""
        assert "max-width" in NEVER_STRIPPED_PROPERTIES
        assert "max-width" not in DIMENSION_PROPERTIES

    def test_dimension_set_is_exact(self) -> None:
        """DIMENSION_PROPERTIES has exactly the specified members."""
        assert {
            "width",
            "height",
            "min-width",
            "min-height",
            "max-height",
        } == DIMENSION_PROPERTIES

    def test_font_set_includes_the_shorthand(self) -> None:
        """FONT_PROPERTIES includes both font-family and the font shorthand."""
        assert {"font-family", "font"} == FONT_PROPERTIES

    def test_color_sets_split_shorthand_out(self) -> None:
        """COLOR_PROPERTIES and COLOR_SHORTHAND_PROPERTIES are distinct."""
        assert {"color", "background-color"} == COLOR_PROPERTIES
        assert {"background"} == COLOR_SHORTHAND_PROPERTIES

    def test_box_properties_cover_all_sides(self) -> None:
        """BOX_PROPERTIES has 11 members covering margin, padding and text-indent."""
        assert len(BOX_PROPERTIES) == 11
        assert "margin" in BOX_PROPERTIES
        assert "margin-left" in BOX_PROPERTIES
        assert "padding" in BOX_PROPERTIES
        assert "padding-bottom" in BOX_PROPERTIES
        assert "text-indent" in BOX_PROPERTIES


class TestPreservedSelectors:
    """Tests for is_preserved_selector predicate."""

    def test_preserved_selectors(self) -> None:
        """is_preserved_selector returns True for drop-cap and special-context selectors."""
        preserved = [
            "p::first-letter",
            "p:first-letter",
            "P::FIRST-LINE",
            ".dropcap",
            ".drop-cap",
            ".initial-cap",
            "span.first-letter",
            "svg",
            "div svg",
            "math",
            ".foo image",
        ]
        for selector in preserved:
            assert is_preserved_selector(selector) is True, f"Failed for {selector}"

    def test_not_preserved_selectors(self) -> None:
        """is_preserved_selector returns False for regular and non-matching selectors."""
        not_preserved = [
            "p",
            "body",
            ".chapter",
            "h1, h2",
            "div.imagery",  # word boundary stops imagery matching image
            ".mathematics",
        ]
        for selector in not_preserved:
            assert is_preserved_selector(selector) is False, f"Failed for {selector}"


class TestImageSelectors:
    """Tests for is_image_selector predicate."""

    def test_image_selectors(self) -> None:
        """is_image_selector returns True for image-related selectors."""
        image_selectors = [
            "img",
            "IMG.center",
            "figure",
            ".cover",
            ".cover-page",
            ".titlepage",
            ".title-page",
            "svg",
        ]
        for selector in image_selectors:
            assert is_image_selector(selector) is True, f"Failed for {selector}"

    def test_not_image_selectors(self) -> None:
        """is_image_selector returns False for non-image selectors."""
        not_image = [
            "p",
            "body",
        ]
        for selector in not_image:
            assert is_image_selector(selector) is False, f"Failed for {selector}"

    def test_covert_is_true_deliberate_overmatch(self) -> None:
        """.covert matches .cover as a deliberate, pinned over-match."""
        assert is_image_selector(".covert") is True

    def test_imaginary_is_not_matched(self) -> None:
        """div.imaginary does not match the image selector (word boundary)."""
        assert is_image_selector("div.imaginary") is False


class TestRootSelectors:
    """Tests for is_root_selector predicate."""

    def test_root_selectors(self) -> None:
        """is_root_selector returns True for html and body at selector position."""
        root_selectors = [
            "html",
            "body",
            "body.fff",
            "html, body",
            "body > p",
            "  body  ",
        ]
        for selector in root_selectors:
            assert is_root_selector(selector) is True, f"Failed for {selector}"

    def test_not_root_selectors(self) -> None:
        """is_root_selector returns False for non-root and class/id names."""
        not_root = [
            "p",
            ".body",
            "bodytext",
            "#body",
            "div.htmlish",
        ]
        for selector in not_root:
            assert is_root_selector(selector) is False, f"Failed for {selector}"


class TestFontMediaTypesAndExtensions:
    """Tests for FONT_MEDIA_TYPES and FONT_EXTENSIONS."""

    def test_font_media_types_and_extensions(self) -> None:
        """Font media types and extensions include expected MIME types and extensions."""
        assert "font/woff2" in FONT_MEDIA_TYPES
        assert "application/vnd.ms-opentype" in FONT_MEDIA_TYPES
        assert ".ttf" in FONT_EXTENSIONS
        assert ".woff2" in FONT_EXTENSIONS
        assert ".jpg" not in FONT_EXTENSIONS


class TestRegexFlags:
    """Tests for regex compilation flags."""

    def test_every_regex_is_case_insensitive(self) -> None:
        """All selector regexes are compiled with re.IGNORECASE flag."""
        assert PRESERVE_SELECTOR_RE.flags & re.IGNORECASE
        assert IMAGE_SELECTOR_RE.flags & re.IGNORECASE
        assert ROOT_SELECTOR_RE.flags & re.IGNORECASE


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
        """ALIGNMENT_PROPERTIES includes text-align and text-indent."""
        assert {"text-align", "text-indent"} == ALIGNMENT_PROPERTIES
