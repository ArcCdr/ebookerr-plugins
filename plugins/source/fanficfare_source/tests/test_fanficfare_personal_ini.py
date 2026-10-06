"""Tests for personal.ini removal in FanFicFareSourcePlugin (D54)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from fanficfare_source.plugin import FanFicFareSourcePlugin, remove_leftover_personal_ini


def test_a_leftover_personal_ini_is_removed_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A leftover personal.ini is deleted on the first call; a second adds no log."""
    caplog.set_level(logging.INFO)

    # Write a personal.ini file
    personal_ini = tmp_path / "personal.ini"
    personal_ini.write_text("[defaults]\nmax_request_retries: 0\n", encoding="utf-8")

    # Call with env var set
    with patch.dict("os.environ", {"EBOOKERR_PLUGIN_DATA_DIR": str(tmp_path)}):
        remove_leftover_personal_ini()

    # File should be gone
    assert not personal_ini.exists()

    # Should have one INFO log
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "INFO"
    assert f"personal.ini is no longer used; removed {personal_ini}" in caplog.text

    # Clear logs
    caplog.clear()

    # Second call should add no record
    with patch.dict("os.environ", {"EBOOKERR_PLUGIN_DATA_DIR": str(tmp_path)}):
        remove_leftover_personal_ini()

    assert len(caplog.records) == 0


def test_no_personal_ini_means_no_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """When there is no personal.ini, no log is recorded."""
    caplog.set_level(logging.INFO)

    with patch.dict("os.environ", {"EBOOKERR_PLUGIN_DATA_DIR": str(tmp_path)}):
        remove_leftover_personal_ini()

    assert len(caplog.records) == 0


def test_the_engine_removes_the_leftover(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Calling _engine() removes a leftover personal.ini."""
    caplog.set_level(logging.INFO)

    # Write a personal.ini file
    personal_ini = tmp_path / "personal.ini"
    personal_ini.write_text("[defaults]\nmax_request_retries: 0\n", encoding="utf-8")

    # Call _engine with env var set
    with patch.dict("os.environ", {"EBOOKERR_PLUGIN_DATA_DIR": str(tmp_path)}):
        plugin = FanFicFareSourcePlugin()
        plugin._engine(None)

    # File should be gone
    assert not personal_ini.exists()

    # Should have logged the removal
    assert any("personal.ini is no longer used" in record.message for record in caplog.records)
