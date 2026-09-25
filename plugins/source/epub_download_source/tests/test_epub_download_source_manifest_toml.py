"""Test that the EPUB download Source's manifest.toml is valid and complete."""

from __future__ import annotations

from ebookerr_sdk.spi import PluginManifest
from epub_download_source.plugin import EpubDownloadSourcePlugin


def test_the_manifest_is_wired_to_the_class() -> None:
    """The plugin's class-level manifest attribute exists and is a PluginManifest."""
    manifest = EpubDownloadSourcePlugin.manifest
    assert isinstance(manifest, PluginManifest)
    assert manifest.id == "epub_download_source"


def test_the_manifest_declares_a_source_plugin_type() -> None:
    """The manifest declares the plugin type as SOURCE."""
    manifest = EpubDownloadSourcePlugin.manifest
    assert manifest.plugin_type.name == "SOURCE"


def test_the_manifest_has_a_non_empty_url_patterns_tuple() -> None:
    """The manifest has a non-empty url_patterns tuple."""
    manifest = EpubDownloadSourcePlugin.manifest
    assert isinstance(manifest.url_patterns, tuple)
    assert len(manifest.url_patterns) > 0


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``epub`` format out of
    process."""
    manifest = EpubDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("epub",)
