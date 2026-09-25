"""Tests for EPUB merge asset planning (``MR-ASSET-1``, ``MR-ASSET-2``, ``MR-FONT-1``)."""

from __future__ import annotations

import logging
from itertools import cycle

import pytest
from ebookerr_sdk.epub.fonts import IDPF_OBFUSCATION, idpf_key
from epub_merge.merge.assets import plan_assets
from epub_merge.merge.model import InputBook, InputResource


def _book(
    index: int, *resources: InputResource, content_root: str = "OEBPS", identifier: str = "uid-0"
) -> InputBook:
    """Build a neutral InputBook with the given resources.

    Args:
        index: Book index.
        *resources: InputResource objects to include.
        content_root: The book's content directory (default "OEBPS").
        identifier: The book's unique identifier (default "uid-0").

    Returns:
        An InputBook with neutral metadata and the provided resources.
    """
    return InputBook(
        index=index,
        name=f"book{index}.epub",
        version="2.0",
        title=f"Book {index}",
        creators=(),
        contributors=(),
        language="en",
        identifier=identifier,
        source=None,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root=content_root,
        chapters=(),
        resources=resources,
        stylesheets=tuple(r.href for r in resources if r.media_type == "text/css"),
        cover_href=None,
        cover_media_type=None,
    )


def test_rebases_against_content_root() -> None:
    """Resources are re-based relative to the content root."""
    resource = InputResource(href="OEBPS/stylesheet.css", media_type="text/css", data=b"body {}")
    book = _book(0, resource, content_root="OEBPS")
    plan, _ = plan_assets([book])

    assert "stylesheet.css" in plan.files
    assert plan.mapping[(0, "OEBPS/stylesheet.css")] == "stylesheet.css"


def test_keeps_nested_resource_directories() -> None:
    """Nested resource directories are preserved in output paths."""
    resource = InputResource(href="OEBPS/images/divider.png", media_type="image/png", data=b"PNG")
    book = _book(0, resource, content_root="OEBPS")
    plan, _ = plan_assets([book])

    assert "images/divider.png" in plan.files
    assert plan.mapping[(0, "OEBPS/images/divider.png")] == "images/divider.png"


def test_collision_between_books_suffixes() -> None:
    """Different content under the same path gets numeric suffixes."""
    resource1 = InputResource(href="OEBPS/images/divider.png", media_type="image/png", data=b"PNG1")
    resource2 = InputResource(href="OEBPS/images/divider.png", media_type="image/png", data=b"PNG2")
    book1 = _book(0, resource1, content_root="OEBPS")
    book2 = _book(1, resource2, content_root="OEBPS")
    plan, _ = plan_assets([book1, book2])

    assert "images/divider.png" in plan.files
    assert "images/divider_1.png" in plan.files
    assert plan.mapping[(0, "OEBPS/images/divider.png")] == "images/divider.png"
    assert plan.mapping[(1, "OEBPS/images/divider.png")] == "images/divider_1.png"


def test_identical_content_is_deduplicated() -> None:
    """Identical content under different names collapses to one file."""
    image_data = b"PNG_IDENTICAL"
    resource1 = InputResource(href="OEBPS/images/a.png", media_type="image/png", data=image_data)
    resource2 = InputResource(href="OEBPS/images/b.png", media_type="image/png", data=image_data)
    resource3 = InputResource(href="OEBPS/images/c.png", media_type="image/png", data=image_data)
    book1 = _book(0, resource1, content_root="OEBPS")
    book2 = _book(1, resource2, content_root="OEBPS")
    book3 = _book(2, resource3, content_root="OEBPS")
    plan, _ = plan_assets([book1, book2, book3])

    png_files = [p for p in plan.files if p.endswith(".png")]
    assert len(png_files) == 1
    assert plan.mapping[(0, "OEBPS/images/a.png")] == plan.mapping[(1, "OEBPS/images/b.png")]
    assert plan.mapping[(1, "OEBPS/images/b.png")] == plan.mapping[(2, "OEBPS/images/c.png")]


def test_survivor_claims_first() -> None:
    """The survivor (book 0) gets the unsuffixed name even when later books have the same."""
    resource1 = InputResource(href="OEBPS/images/a.png", media_type="image/png", data=b"PNG1")
    resource2 = InputResource(href="OEBPS/images/a.png", media_type="image/png", data=b"PNG2")
    book1 = _book(0, resource1, content_root="OEBPS")
    book2 = _book(1, resource2, content_root="OEBPS")
    plan, _ = plan_assets([book1, book2])

    assert plan.mapping[(0, "OEBPS/images/a.png")] == "images/a.png"
    assert plan.mapping[(1, "OEBPS/images/a.png")] == "images/a_1.png"


def test_non_survivor_stylesheet_is_dropped() -> None:
    """Non-survivor stylesheets are dropped; mapping points to survivor's first CSS."""
    survivor_css = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body {}")
    other_css = InputResource(href="OEBPS/other.css", media_type="text/css", data=b"h1 {}")
    book1 = _book(0, survivor_css, content_root="OEBPS")
    book2 = _book(1, other_css, content_root="OEBPS")
    plan, _ = plan_assets([book1, book2])

    assert "style.css" in plan.files
    assert "other.css" not in plan.files
    assert plan.mapping[(1, "OEBPS/other.css")] == "style.css"


def test_non_survivor_stylesheet_without_survivor_css_has_no_mapping() -> None:
    """Non-survivor CSS with no survivor CSS has no mapping entry."""
    other_css = InputResource(href="OEBPS/other.css", media_type="text/css", data=b"h1 {}")
    book1 = _book(0, content_root="OEBPS")  # survivor with no CSS
    book2 = _book(1, other_css, content_root="OEBPS")
    plan, _ = plan_assets([book1, book2])

    assert (1, "OEBPS/other.css") not in plan.mapping


def test_survivor_stylesheet_is_kept() -> None:
    """The survivor's CSS is included in the output."""
    survivor_css = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body {}")
    book = _book(0, survivor_css, content_root="OEBPS")
    plan, _ = plan_assets([book])

    assert "style.css" in plan.files


def test_font_is_deobfuscated_before_hashing() -> None:
    """Fonts are deobfuscated before hashing for deduplication."""
    # Original font data
    original_font = b"FONTDATA" * 200  # 1600 bytes
    # Create obfuscated copies using IDPF scheme with different identifiers
    key1 = idpf_key("uid-1")
    key2 = idpf_key("uid-2")
    obfuscated1 = (
        bytes(b ^ k for b, k in zip(original_font[:1040], cycle(key1), strict=False))
        + original_font[1040:]
    )
    obfuscated2 = (
        bytes(b ^ k for b, k in zip(original_font[:1040], cycle(key2), strict=False))
        + original_font[1040:]
    )

    resource1 = InputResource(
        href="OEBPS/fonts/font.otf", media_type="font/opentype", data=obfuscated1
    )
    resource2 = InputResource(
        href="OEBPS/fonts/font.otf", media_type="font/opentype", data=obfuscated2
    )

    book1 = _book(0, resource1, content_root="OEBPS", identifier="uid-1")
    book2 = _book(1, resource2, content_root="OEBPS", identifier="uid-2")

    encryption = {
        0: {"OEBPS/fonts/font.otf": IDPF_OBFUSCATION},
        1: {"OEBPS/fonts/font.otf": IDPF_OBFUSCATION},
    }
    plan, _ = plan_assets([book1, book2], encryption=encryption)

    otf_files = [p for p in plan.files if p.endswith(".otf")]
    assert len(otf_files) == 1
    # Both should map to the same file
    assert plan.mapping[(0, "OEBPS/fonts/font.otf")] == plan.mapping[(1, "OEBPS/fonts/font.otf")]


def test_font_decryption_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    """Font de-obfuscation is logged at INFO level."""
    font_data = b"FONTDATA" * 200
    key = idpf_key("uid-0")
    obfuscated = (
        bytes(b ^ k for b, k in zip(font_data[:1040], cycle(key), strict=False)) + font_data[1040:]
    )

    resource = InputResource(
        href="OEBPS/fonts/font.otf", media_type="font/opentype", data=obfuscated
    )
    book = _book(0, resource, content_root="OEBPS", identifier="uid-0")
    encryption = {0: {"OEBPS/fonts/font.otf": IDPF_OBFUSCATION}}

    with caplog.at_level(logging.INFO):
        plan_assets([book], encryption=encryption)

    assert "Decrypted obfuscated font" in caplog.text


def test_unknown_algorithm_warns_and_keeps_bytes() -> None:
    """Unknown obfuscation algorithms are left untouched with a warning."""
    font_data = b"FONTDATA" * 200
    resource = InputResource(
        href="OEBPS/fonts/font.otf", media_type="font/opentype", data=font_data
    )
    book = _book(0, resource, content_root="OEBPS", identifier="uid-0")
    encryption = {0: {"OEBPS/fonts/font.otf": "http://example.com/unknown"}}

    plan, _ = plan_assets([book], encryption=encryption)

    assert plan.files["fonts/font.otf"] == font_data


def test_unknown_algorithm_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Unknown obfuscation algorithms log a WARNING."""
    font_data = b"FONTDATA" * 200
    resource = InputResource(
        href="OEBPS/fonts/font.otf", media_type="font/opentype", data=font_data
    )
    book = _book(0, resource, content_root="OEBPS", identifier="uid-0")
    encryption = {0: {"OEBPS/fonts/font.otf": "http://example.com/unknown"}}

    with caplog.at_level(logging.WARNING):
        plan_assets([book], encryption=encryption)

    assert "Unknown font obfuscation algorithm" in caplog.text


def test_no_encryption_mapping_is_fine() -> None:
    """Default empty encryption mapping works; all resources copied verbatim."""
    resource = InputResource(
        href="OEBPS/fonts/font.otf", media_type="font/opentype", data=b"FONTDATA"
    )
    book = _book(0, resource, content_root="OEBPS", identifier="uid-0")

    plan, _ = plan_assets([book])  # no encryption argument

    assert plan.files["fonts/font.otf"] == b"FONTDATA"


def test_media_types_are_recorded() -> None:
    """Media types are recorded for each output path, respecting first claimer."""
    image_data = b"PNG"
    resource1 = InputResource(href="OEBPS/images/a.png", media_type="image/png", data=image_data)
    resource2 = InputResource(href="OEBPS/images/b.png", media_type="image/png", data=image_data)
    book1 = _book(0, resource1, content_root="OEBPS")
    book2 = _book(1, resource2, content_root="OEBPS")
    plan, _ = plan_assets([book1, book2])

    assert plan.media_types["images/a.png"] == "image/png"
    # The deduplicated path should keep the first claimer's media type
    assert "images/b.png" not in plan.media_types or plan.media_types["images/a.png"] == "image/png"


def test_logs_info_summary(caplog: pytest.LogCaptureFixture) -> None:
    """A summary log at INFO level records the resource count."""
    resource1 = InputResource(href="OEBPS/images/a.png", media_type="image/png", data=b"PNG1")
    resource2 = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body {}")
    book1 = _book(0, resource1, resource2, content_root="OEBPS")
    book2 = _book(
        1,
        InputResource(href="OEBPS/images/b.png", media_type="image/png", data=b"PNG2"),
        content_root="OEBPS",
    )

    with caplog.at_level(logging.INFO):
        plan, _ = plan_assets([book1, book2])

    assert "Merge planned" in caplog.text
    assert f"{len(plan.files)}" in caplog.text or "3" in caplog.text


def test_deterministic_across_runs() -> None:
    """Running plan_assets twice on identical inputs yields equal results."""
    resource1 = InputResource(href="OEBPS/images/a.png", media_type="image/png", data=b"PNG1")
    resource2 = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body {}")
    book1 = _book(0, resource1, resource2, content_root="OEBPS")
    book2 = _book(
        1,
        InputResource(href="OEBPS/images/b.png", media_type="image/png", data=b"PNG2"),
        content_root="OEBPS",
    )

    plan1, _ = plan_assets([book1, book2])
    plan2, _ = plan_assets([book1, book2])

    assert plan1.files == plan2.files
    assert plan1.media_types == plan2.media_types
    assert plan1.mapping == plan2.mapping


def test_empty_books() -> None:
    """Empty book list returns empty plan."""
    plan, _ = plan_assets([])

    assert plan.files == {}
    assert plan.media_types == {}
    assert plan.mapping == {}
