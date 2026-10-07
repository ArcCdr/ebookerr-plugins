"""Tests for the URL Story Extractor plugin."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_the_manifest_declares_the_story_extractor_role() -> None:
    """The manifest declares the story-extractor role with url patterns and settings reference."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    with manifest_path.open("rb") as f:
        data = tomllib.load(f)

    assert "roles" in data
    assert "story_extractor" in data["roles"]
    assert data["roles"]["story_extractor"] == {
        "url_patterns": ["^https?://"],
        "urls_setting": "extract_urls",
    }
    assert "extract_url_patterns" not in data
    assert data["spi_version"] == "3.0"


def test_the_extractor_requires_fanficfare_4_62() -> None:
    """The manifest requires FanFicFare 4.62.0 or later."""
    from url_story_extractor.plugin import UrlStoryExtractorPlugin

    assert UrlStoryExtractorPlugin.manifest.requirements == ("fanficfare>=4.62.0",)


def test_the_extractor_is_version_1_3_0() -> None:
    """The 2.22.1 release of the URL Story Extractor is 1.3.0."""
    from url_story_extractor.plugin import UrlStoryExtractorPlugin

    assert UrlStoryExtractorPlugin.manifest.version == "1.3.0"


def test_the_extractor_declares_its_fanficfare_settings() -> None:
    """The extractor has its own FanFicFare settings and receives any site's sign-in (C36, D55)."""
    from url_story_extractor.plugin import UrlStoryExtractorPlugin

    manifest = UrlStoryExtractorPlugin.manifest
    last_two = manifest.settings_schema.fields[-2:]

    assert [field.key for field in last_two] == ["is_adult", "extra_options"]
    assert [field.type for field in last_two] == ["bool", "textarea"]
    assert [field.default for field in last_two] == [True, ""]
    assert manifest.auth_sites == ("*",)
