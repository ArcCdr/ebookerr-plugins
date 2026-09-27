"""TOML manifest validation tests."""

from __future__ import annotations

from kavita_sync.plugin import KavitaSyncPlugin


def test_the_provider_asks_for_the_network_and_names_its_required_settings() -> None:
    """An exec BOOK plugin reaches its server only when it declares ``network`` (PMG-FR-49)."""
    manifest = KavitaSyncPlugin.manifest
    assert manifest.network is True
    assert [f.key for f in manifest.settings_schema.fields if f.required] == ["server", "api_key"]


def test_the_manifest_declares_the_library_server_role() -> None:
    """The manifest's roles.library_server table equals expected values, no legacy top-level keys, spi_version == 2.32."""
    manifest = KavitaSyncPlugin.manifest
    assert manifest.roles.library_server == {
        "provider": "kavita",
        "delete_mode": "rescan",
        "reader_url_template": "{external_url}/library/{library_id}/series/{series_id}/book/{book_id}",
        "server_url_setting": "server",
        "public_url_setting": "external_url",
    }
    assert not hasattr(manifest, "exclusive_group") or manifest.exclusive_group is None
    assert manifest.spi_version == "2.32"
