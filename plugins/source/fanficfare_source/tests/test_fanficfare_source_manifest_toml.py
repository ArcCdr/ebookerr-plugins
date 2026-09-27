"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations


def test_the_source_answers_update_checks_and_is_the_catch_all() -> None:
    """The manifest declares update_check and fallback_source role, and fanficfare requirement."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    assert FanFicFareSourcePlugin.manifest.update_check is True
    assert FanFicFareSourcePlugin.manifest.roles.fallback_source is True
    assert FanFicFareSourcePlugin.manifest.requirements == ("fanficfare>=4.58.1",)
