"""Opt-in E2E: a real FanFicFare download through the library gateway (network, LIB-D25).

Deselected by default (``-m 'not e2e'``); run with ``python -m pytest -m e2e`` from the plugins
repository. Writes the EPUB to a temp dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fanficfare_source.library import FanFicFareLibraryGateway

pytestmark = pytest.mark.e2e

URL = "https://literotica.com/s/the-12th-key"
PERSONAL_INI = Path(__file__).resolve().parents[1] / "fanficfare_source" / "personal.ini"


def test_real_fanficfare_download(tmp_path: Path) -> None:
    """A real story downloads into a fresh EPUB with its metadata."""
    gateway = FanFicFareLibraryGateway(PERSONAL_INI)
    result = gateway.download(URL, work_dir=tmp_path, staged_filename=None)
    assert result.outcome == "created", result.error
    assert result.output_filename
    assert (tmp_path / result.output_filename).is_file()
    assert result.json_data["title"]
    assert result.json_data["author"]


def test_fetch_metadata_writes_no_epub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_metadata returns the story's metadata and writes no EPUB."""
    monkeypatch.chdir(tmp_path)
    meta = FanFicFareLibraryGateway(PERSONAL_INI).fetch_metadata(URL)
    assert meta is not None
    assert meta["numChapters"]
    assert list(tmp_path.glob("**/*.epub")) == []


def test_a_real_update_fetches_nothing_new(tmp_path: Path) -> None:
    """Updating a story just downloaded adds no chapter and keeps every one (LIB-D27)."""
    gateway = FanFicFareLibraryGateway(PERSONAL_INI)
    first = gateway.download(URL, work_dir=tmp_path, staged_filename=None)
    assert first.outcome == "created", first.error
    again = gateway.download(URL, work_dir=tmp_path, staged_filename=first.output_filename)
    assert again.outcome == "updated", again.error
    assert again.added == 0
    assert again.chapters_after == again.chapters_before
