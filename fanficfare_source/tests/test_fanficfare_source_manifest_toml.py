"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

import pytest
from ebookerr_sdk.spi.manifest import parse_manifest

_MANIFEST_TOML = Path(__file__).resolve().parents[1] / "manifest.toml"


def test_fanficfare_source_manifest_matches_the_in_image_manifest() -> None:
    """``manifest.toml`` parses to the class's own manifest; only the transport differs."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    parsed = parse_manifest(tomllib.loads(_MANIFEST_TOML.read_text(encoding="utf-8")))
    assert parsed == dataclasses.replace(FanFicFareSourcePlugin.manifest, transport="local_exec")


def test_the_source_answers_update_checks_and_is_the_catch_all() -> None:
    """The manifest declares update_check, catch_all, and fanficfare requirement."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    assert FanFicFareSourcePlugin.manifest.update_check is True
    assert FanFicFareSourcePlugin.manifest.catch_all is True
    assert FanFicFareSourcePlugin.manifest.requirements == ("fanficfare>=4.58.1",)
