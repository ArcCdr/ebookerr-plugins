"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from ebookerr_sdk.spi.manifest import parse_manifest
from epub_normalize.plugin import EpubNormalizePlugin

_MANIFEST_TOML = Path(__file__).resolve().parents[1] / "manifest.toml"


def test_epub_normalize_manifest_matches_the_in_image_manifest() -> None:
    """``manifest.toml`` parses to the class's own manifest; the two are equal."""
    parsed = parse_manifest(tomllib.loads(_MANIFEST_TOML.read_text(encoding="utf-8")))
    assert parsed == EpubNormalizePlugin.manifest
