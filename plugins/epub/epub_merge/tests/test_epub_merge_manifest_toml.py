"""TOML manifest validation tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

from epub_merge.plugin import EpubMergePlugin


def test_the_manifest_declares_the_merge_proposals_role() -> None:
    """The manifest declares merge_proposals role in the new format."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

    # Verify the role is declared
    assert data["roles"] == {"merge_proposals": {}}

    # Verify legacy top-level key is gone
    assert "handles_merge_proposals" not in data

    # Verify SPI version
    assert data["spi_version"] == "3.1"


def test_the_merge_action_shows_the_plugin_glyph() -> None:
    """The Merge button has no icon of its own, so it shows the plugin's glyph."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

    assert "icon" not in data["ui_triggers"][0]
    assert EpubMergePlugin().manifest.ui_triggers[0].icon == "merge"
