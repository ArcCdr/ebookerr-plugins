"""TOML manifest validation tests."""

from __future__ import annotations

from kavita_sync.plugin import KavitaSyncPlugin


def test_the_provider_asks_for_the_network_and_names_its_required_settings() -> None:
    """An exec BOOK plugin reaches its server only when it declares ``network`` (PMG-FR-49)."""
    manifest = KavitaSyncPlugin.manifest
    assert manifest.network is True
    assert [f.key for f in manifest.settings_schema.fields if f.required] == ["server", "api_key"]


def test_the_manifest_declares_the_library_server_role() -> None:
    """The manifest declares the library_server role with expected values and spi_version 2.33."""
    manifest = KavitaSyncPlugin.manifest
    role = manifest.roles.library_server
    assert role is not None
    assert role.provider == "kavita"
    assert role.delete_mode == "rescan"
    reader_url = "{external_url}/library/{library_id}/series/{series_id}/book/{book_id}"
    assert role.reader_url_template == reader_url
    assert role.server_url_setting == "server"
    assert role.public_url_setting == "external_url"
    assert manifest.spi_version == "2.33"
    assert role.live_read_state is True
