"""Tests for the URL Story Extractor plugin."""

from __future__ import annotations

import re
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


def test_the_extractor_pins_fanficfare_exactly() -> None:
    """The manifest pins one exact FanFicFare version (a dependency release moves it)."""
    from url_story_extractor.plugin import UrlStoryExtractorPlugin

    (requirement,) = UrlStoryExtractorPlugin.manifest.requirements
    assert re.fullmatch(r"fanficfare==\d+(\.\d+)+", requirement)


def test_the_version_is_the_top_changelog_entry() -> None:
    """The manifest version is the newest CHANGELOG entry's (a dependency release moves both)."""
    from url_story_extractor.plugin import UrlStoryExtractorPlugin

    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.txt"
    first_line = changelog.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == f"== {UrlStoryExtractorPlugin.manifest.version} =="


def test_the_extractor_declares_its_fanficfare_settings() -> None:
    """The extractor has its own FanFicFare settings and receives any site's sign-in (C36, D55)."""
    from url_story_extractor.plugin import UrlStoryExtractorPlugin

    manifest = UrlStoryExtractorPlugin.manifest
    last_two = manifest.settings_schema.fields[-2:]

    assert [field.key for field in last_two] == ["is_adult", "extra_options"]
    assert [field.type for field in last_two] == ["bool", "textarea"]
    assert [field.default for field in last_two] == [True, ""]
    assert manifest.auth_sites == ("*",)
