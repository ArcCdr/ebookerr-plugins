"""TOML manifest validation tests."""

from __future__ import annotations

from kavita_sync.plugin import KavitaSyncPlugin


def test_the_provider_asks_for_the_network_and_names_its_required_settings() -> None:
    """An exec BOOK plugin reaches its server only when it declares ``network`` (PMG-FR-49)."""
    manifest = KavitaSyncPlugin.manifest
    assert manifest.network is True
    assert [f.key for f in manifest.settings_schema.fields if f.required] == ["server", "api_key"]
