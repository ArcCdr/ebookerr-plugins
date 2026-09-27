"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

from text_download_source.plugin import TextDownloadSourcePlugin


def test_the_manifest_opts_into_update_checks_and_its_format() -> None:
    """The Source answers the Auto-Pull update check and claims the ``txt`` format out of
    process."""
    manifest = TextDownloadSourcePlugin.manifest
    assert manifest.update_check is True
    assert manifest.formats == ("txt",)
