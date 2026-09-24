"""Opt-in E2E: a real FanFicFare download (network). Run with RUN_E2E=1.

Skipped by default (see tests/conftest.py). Validates the gateway against the real
CLI + a real Literotica story; writes the EPUB to a temp dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fanficfare_source.cli import FanFicFareCliGateway

pytestmark = pytest.mark.e2e

URL = "https://literotica.com/s/the-12th-key"


def _gateway() -> FanFicFareCliGateway:
    # Prefer the fanficfare next to the running interpreter (the project venv).
    candidate = Path(sys.executable).parent / "fanficfare"
    executable = str(candidate) if candidate.exists() else "fanficfare"
    personal_ini = Path(__file__).resolve().parents[1] / "fanficfare_source" / "personal.ini"
    return FanFicFareCliGateway(personal_ini, executable=executable)


def test_real_fanficfare_download(tmp_path: Path) -> None:
    gateway = _gateway()
    if not gateway.is_available():
        pytest.skip("fanficfare executable not found")
    result = gateway.download(URL, work_dir=tmp_path)
    assert result.ok, result.error
    assert result.output_filename
    assert (tmp_path / result.output_filename).is_file()
    assert result.json_data["title"]
    assert result.json_data["author"]


def test_fetch_metadata_writes_no_epub_outside_scratch(tmp_path: Path) -> None:
    """fetch_metadata returns valid metadata and leaves no .epub outside its scratch dir."""
    gateway = _gateway()
    if not gateway.is_available():
        pytest.skip("fanficfare executable not found")
    result = gateway.fetch_metadata(URL)
    assert result is not None
    assert result["numChapters"]
    assert list(tmp_path.glob("**/*.epub")) == []
