"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

from ebookerr_sdk.spi import Converting
from text_download_source.plugin import TextDownloadSourcePlugin


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``txt`` format out of
    process."""
    manifest = TextDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("txt",)


def test_the_manifest_declares_the_converter_role() -> None:
    """The Source converts uploaded txt files: [roles.converter], formats unchanged, SPI 3.0."""
    manifest = TextDownloadSourcePlugin.manifest
    assert manifest.roles.converter is True
    assert manifest.formats == ("txt",)
    assert manifest.spi_version == "3.0"
    assert isinstance(TextDownloadSourcePlugin(), Converting)


def test_the_source_receives_the_sign_in_of_any_site() -> None:
    """The Source receives the sign-in of the site it downloads from (auth_sites = [\"*\"])."""
    manifest = TextDownloadSourcePlugin.manifest
    assert manifest.auth_sites == ("*",)
