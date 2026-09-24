"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

from ebookerr_sdk.spi.manifest import parse_manifest
from kavita_sync.plugin import KavitaSyncPlugin

_MANIFEST_TOML = Path(__file__).resolve().parents[1] / "manifest.toml"


def test_kavita_sync_manifest_matches_the_in_image_manifest() -> None:
    """``manifest.toml`` parses to the class's own manifest; only the transport differs."""
    parsed = parse_manifest(tomllib.loads(_MANIFEST_TOML.read_text(encoding="utf-8")))
    assert parsed == dataclasses.replace(KavitaSyncPlugin.manifest, transport="local_exec")


def test_the_provider_asks_for_the_network_and_names_its_required_settings() -> None:
    """An exec BOOK plugin reaches its server only when it declares ``network`` (PMG-FR-49)."""
    manifest = KavitaSyncPlugin.manifest
    assert manifest.network is True
    assert [f.key for f in manifest.settings_schema.fields if f.required] == ["server", "api_key"]
