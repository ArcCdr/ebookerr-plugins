"""TOML manifest validation tests."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_the_manifest_declares_the_merge_proposals_role() -> None:
    """The manifest declares merge_proposals role in the new format."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

    # Verify the role is declared
    assert data["roles"] == {"merge_proposals": {}}

    # Verify legacy top-level key is gone
    assert "handles_merge_proposals" not in data

    # Verify SPI version
    assert data["spi_version"] == "2.32"
