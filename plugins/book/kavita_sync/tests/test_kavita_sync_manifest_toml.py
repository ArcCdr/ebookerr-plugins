"""TOML manifest validation tests."""

from __future__ import annotations

from kavita_sync.plugin import KavitaSyncPlugin


def test_the_provider_names_its_required_settings() -> None:
    """The provider declares required settings."""
    manifest = KavitaSyncPlugin.manifest
    assert [f.key for f in manifest.settings_schema.fields if f.required] == ["server", "api_key"]


def test_the_manifest_declares_the_library_server_role() -> None:
    """The manifest declares the library_server role with expected values and spi_version 3.0."""
    manifest = KavitaSyncPlugin.manifest
    role = manifest.roles.library_server
    assert role is not None
    assert role.provider == "kavita"
    assert role.delete_mode == "rescan"
    reader_url = "{external_url}/library/{library_id}/series/{series_id}/book/{book_id}"
    assert role.reader_url_template == reader_url
    assert role.server_url_setting == "server"
    assert role.public_url_setting == "external_url"
    assert manifest.spi_version == "3.1"
    assert role.live_read_state is True
    assert role.write_read_state is True
