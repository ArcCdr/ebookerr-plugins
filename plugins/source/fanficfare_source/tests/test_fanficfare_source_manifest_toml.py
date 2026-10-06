"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_the_manifest_declares_the_fallback_source_role() -> None:
    """The manifest declares the fallback_source role and SPI 3.0, no legacy catch_all."""
    manifest_path = Path(__file__).parent.parent / "manifest.toml"
    with manifest_path.open("rb") as f:
        data = tomllib.load(f)

    assert data["roles"] == {"fallback_source": {}}
    assert "catch_all" not in data
    assert data["spi_version"] == "3.0"


def test_the_source_answers_update_checks_and_is_the_catch_all() -> None:
    """The manifest declares update_check and fallback_source role, and fanficfare requirement."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    assert FanFicFareSourcePlugin.manifest.update_check is True
    assert FanFicFareSourcePlugin.manifest.roles.fallback_source is True
    assert FanFicFareSourcePlugin.manifest.requirements == ("fanficfare>=4.62.0",)


def test_the_source_is_version_1_4_0() -> None:
    """The 2.22.1 release of the FanFicFare Source is 1.4.0."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    assert FanFicFareSourcePlugin.manifest.version == "1.4.0"
