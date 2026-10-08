"""The EPUB download source's manifest glyph (PMG-FR-27)."""

from __future__ import annotations

from pathlib import Path

from ebookerr_sdk.spi.manifest import load_manifest


def test_the_plugin_glyph_is_globe_book() -> None:
    """The Source's glyph is a book with a globe, not the Install button's download."""
    manifest = load_manifest(Path(__file__).resolve().parents[1] / "manifest.toml")
    assert manifest.icon == "globe_book"
    assert manifest.version == "1.2.1"
