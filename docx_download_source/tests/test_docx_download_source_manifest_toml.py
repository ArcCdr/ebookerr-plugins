"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

from docx_download_source.plugin import DocxDownloadSourcePlugin
from ebookerr_sdk.spi.manifest import parse_manifest

_MANIFEST_TOML = Path(__file__).resolve().parents[1] / "manifest.toml"


def test_docx_download_source_manifest_matches_the_in_image_manifest() -> None:
    """``manifest.toml`` parses to the class's own manifest; only the transport differs."""
    parsed = parse_manifest(tomllib.loads(_MANIFEST_TOML.read_text(encoding="utf-8")))
    assert parsed == dataclasses.replace(DocxDownloadSourcePlugin.manifest, transport="local_exec")


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``docx`` format out of
    process."""
    manifest = DocxDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("docx",)
