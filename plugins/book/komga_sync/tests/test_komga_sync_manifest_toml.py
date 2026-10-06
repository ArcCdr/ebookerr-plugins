"""TOML manifest validation tests."""

from __future__ import annotations

import tomllib
from pathlib import Path

from komga_sync.plugin import KomgaSyncPlugin


def test_the_provider_names_its_required_settings() -> None:
    """The provider declares required settings."""
    manifest = KomgaSyncPlugin.manifest
    assert [f.key for f in manifest.settings_schema.fields if f.required] == [
        "server",
        "api_key",
        "library_id",
    ]


def test_the_manifest_declares_the_library_server_role() -> None:
    """The manifest declares library_server role in the new format."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

    # Verify the role is declared
    assert "roles" in data
    assert "library_server" in data["roles"]
    assert data["roles"]["library_server"] == {
        "provider": "komga",
        "delete_mode": "purge",
        "reader_url_template": "{external_url}/book/{book_id}/read-epub",
        "server_url_setting": "server",
        "public_url_setting": "external_url",
        "live_read_state": True,
        "write_read_state": True,
    }

    # Verify legacy top-level keys are gone
    assert "exclusive_group" not in data
    assert data.get("provider") is None or data.get("provider") != "komga"
    assert "delete_mode" not in data or data.get("delete_mode") != "purge"
    assert "reader_url_template" not in data or data.get("reader_url_template") is None

    # Verify SPI version
    assert data["spi_version"] == "3.0"


def test_the_manifest_declares_the_read_state_write_through() -> None:
    """The parsed manifest declares write_read_state (SPI 2.34, RDG-D4)."""
    manifest = KomgaSyncPlugin.manifest
    role = manifest.roles.library_server
    assert role is not None
    assert role.write_read_state is True
    assert manifest.spi_version == "3.0"
