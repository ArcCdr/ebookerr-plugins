"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

from epub_validate.plugin import EpubValidatePlugin


def test_every_validation_value_is_a_check_report() -> None:
    """All epub_validate custom values declare display == 'check_report'."""
    displays = {d.display for d in EpubValidatePlugin.manifest.custom_values}
    assert displays == {"check_report"}
