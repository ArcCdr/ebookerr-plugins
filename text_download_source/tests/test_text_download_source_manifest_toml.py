"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from ebookerr_sdk.spi.manifest import parse_manifest
from text_download_source.plugin import TextDownloadSourcePlugin

_MANIFEST_TOML = Path(__file__).resolve().parents[1] / "manifest.toml"


def test_text_download_source_manifest_matches_the_in_image_manifest() -> None:
    """``manifest.toml`` parses to the class's own manifest; the two are equal."""
    parsed = parse_manifest(tomllib.loads(_MANIFEST_TOML.read_text(encoding="utf-8")))
    assert parsed == TextDownloadSourcePlugin.manifest


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``txt`` format out of
    process."""
    manifest = TextDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("txt",)
