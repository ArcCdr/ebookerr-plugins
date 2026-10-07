"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

from ebookerr_sdk.spi import Converting
from rtf_download_source.plugin import RtfDownloadSourcePlugin


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``rtf`` format out of
    process."""
    manifest = RtfDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("rtf",)


def test_the_manifest_declares_the_converter_role() -> None:
    """The Source converts uploaded rtf files: [roles.converter], formats unchanged, SPI 3.0."""
    manifest = RtfDownloadSourcePlugin.manifest
    assert manifest.roles.converter is True
    assert manifest.formats == ("rtf",)
    assert manifest.spi_version == "3.0"
    assert isinstance(RtfDownloadSourcePlugin(), Converting)


def test_the_source_receives_the_sign_in_of_any_site() -> None:
    """The Source receives the sign-in of the site it downloads from (auth_sites = [\"*\"])."""
    manifest = RtfDownloadSourcePlugin.manifest
    assert manifest.auth_sites == ("*",)
