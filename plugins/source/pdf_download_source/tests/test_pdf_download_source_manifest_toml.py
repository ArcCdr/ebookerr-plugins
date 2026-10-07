"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

from ebookerr_sdk.spi import Converting
from pdf_download_source.plugin import PdfDownloadSourcePlugin


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``pdf`` format out of
    process."""
    manifest = PdfDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("pdf",)


def test_the_manifest_declares_the_converter_role() -> None:
    """The Source converts uploaded pdf files: [roles.converter], formats unchanged, SPI 3.0."""
    manifest = PdfDownloadSourcePlugin.manifest
    assert manifest.roles.converter is True
    assert manifest.formats == ("pdf",)
    assert manifest.spi_version == "3.0"
    assert isinstance(PdfDownloadSourcePlugin(), Converting)


def test_the_source_receives_the_sign_in_of_any_site() -> None:
    """The Source receives the sign-in of the site it downloads from (auth_sites = [\"*\"])."""
    manifest = PdfDownloadSourcePlugin.manifest
    assert manifest.auth_sites == ("*",)


def test_the_licence_is_agpl_3_or_later() -> None:
    """The Source declares the licence of the PDF library it bundles (AGPL-3.0-or-later)."""
    assert PdfDownloadSourcePlugin.manifest.license == "AGPL-3.0-or-later"
