"""Opt-in E2E: a real FanFicFare download through the library gateway (network, LIB-D25).

Deselected by default (``-m 'not e2e'``); run with ``python -m pytest -m e2e`` from the plugins
repository. Writes the EPUB to a temp dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fanficfare_source.library import FanFicFareLibraryGateway

pytestmark = pytest.mark.e2e


def test_real_fanficfare_download(tmp_path: Path) -> None:
    """A real story downloads into a fresh EPUB with its metadata."""
    url = "https://literotica.com/s/the-12th-key"
    gateway = FanFicFareLibraryGateway({"is_adult": True})
    result = gateway.download(url, work_dir=tmp_path, staged_filename=None)
    assert result.outcome == "created", result.error
    assert result.output_filename
    assert (tmp_path / result.output_filename).is_file()
    assert result.json_data["title"]
    assert result.json_data["author"]


def test_fetch_metadata_writes_no_epub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_metadata returns the story's metadata and writes no EPUB."""
    url = "https://literotica.com/s/the-12th-key"
    monkeypatch.chdir(tmp_path)
    meta = FanFicFareLibraryGateway({"is_adult": True}).fetch_metadata(url)
    assert meta is not None
    assert meta["numChapters"]
    assert list(tmp_path.glob("**/*.epub")) == []


def test_a_real_update_fetches_nothing_new(tmp_path: Path) -> None:
    """Updating a story just downloaded adds no chapter and keeps every one (LIB-D27)."""
    url = "https://literotica.com/s/the-12th-key"
    gateway = FanFicFareLibraryGateway({"is_adult": True})
    first = gateway.download(url, work_dir=tmp_path, staged_filename=None)
    assert first.outcome == "created", first.error
    again = gateway.download(url, work_dir=tmp_path, staged_filename=first.output_filename)
    assert again.outcome == "updated", again.error
    assert again.added == 0
    assert again.chapters_after == again.chapters_before
