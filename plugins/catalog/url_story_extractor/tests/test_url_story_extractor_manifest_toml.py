"""Tests for the URL Story Extractor plugin."""

from __future__ import annotations

from pathlib import Path

import pytest
from url_story_extractor.plugin import default_pages


def test_the_default_gateway_reads_the_fanficfare_sources_file_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared site-login file wins over the packaged default (D37)."""
    shared = tmp_path / "fanficfare_source" / "personal.ini"
    shared.parent.mkdir()
    shared.write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "url_story_extractor"))
    assert default_pages()._personal_ini == shared


def test_the_default_gateway_falls_back_to_the_packaged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no shared file, the packaged default beside the plugin is read (D37)."""
    monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "url_story_extractor"))
    packaged = Path(__file__).resolve().parents[1] / "url_story_extractor" / "personal.ini"
    assert default_pages()._personal_ini == packaged
    assert packaged.is_file()
