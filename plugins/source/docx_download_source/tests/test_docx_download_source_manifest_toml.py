"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

from docx_download_source.plugin import DocxDownloadSourcePlugin
from ebookerr_sdk.spi import Converting


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``docx`` format out of
    process."""
    manifest = DocxDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("docx",)


def test_the_manifest_declares_the_converter_role() -> None:
    """The Source converts uploaded docx files: [roles.converter], formats unchanged, SPI 3.0."""
    manifest = DocxDownloadSourcePlugin.manifest
    assert manifest.roles.converter is True
    assert manifest.formats == ("docx",)
    assert manifest.spi_version == "3.0"
    assert isinstance(DocxDownloadSourcePlugin(), Converting)


def test_the_version_is_the_republished_one() -> None:
    """The DOCX download is republished as 1.3.1; 1.2.0 and 1.3.0 never reached the catalogue."""
    manifest = DocxDownloadSourcePlugin.manifest
    assert manifest.version == "1.3.1"


def test_the_source_receives_the_sign_in_of_any_site() -> None:
    """The Source receives the sign-in of the site it downloads from (auth_sites = [\"*\"])."""
    manifest = DocxDownloadSourcePlugin.manifest
    assert manifest.auth_sites == ("*",)
