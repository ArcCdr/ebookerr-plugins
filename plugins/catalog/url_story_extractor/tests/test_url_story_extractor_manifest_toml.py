"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ebookerr_sdk.spi.manifest import parse_manifest
from url_story_extractor.plugin import UrlStoryExtractorPlugin, default_pages

_MANIFEST_TOML = Path(__file__).resolve().parents[1] / "manifest.toml"


def test_url_story_extractor_manifest_matches_the_in_image_manifest() -> None:
    """``manifest.toml`` parses to the class's own manifest; the two are equal."""
    parsed = parse_manifest(tomllib.loads(_MANIFEST_TOML.read_text(encoding="utf-8")))
    assert parsed == UrlStoryExtractorPlugin.manifest


def test_the_default_gateway_reads_the_fanficfare_sources_file_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared site-login file wins over the packaged default (D37)."""
    shared = tmp_path / "fanficfare_source" / "personal.ini"
    shared.parent.mkdir()
    shared.write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "url_story_extractor"))
    assert default_pages()._personal_ini == shared


def test_the_default_gateway_falls_back_to_the_packaged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no shared file, the packaged default beside the plugin is read (D37)."""
    monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "url_story_extractor"))
    packaged = Path(__file__).resolve().parents[1] / "url_story_extractor" / "personal.ini"
    assert default_pages()._personal_ini == packaged
    assert packaged.is_file()
