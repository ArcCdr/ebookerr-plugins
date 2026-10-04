"""The registry index lists only plugins that still have a folder (scripts/indexed_releases.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "indexed_releases.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("indexed_releases", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_plugin_id_is_everything_before_the_last_dash() -> None:
    assert _script().release_plugin_id("epub_merge-2.4.0") == "epub_merge"


def test_a_release_without_a_plugin_folder_is_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    published = {
        "epub_merge-2.3.0": "2026-09-01T10:00:00Z",
        "epub_merge-2.4.0": "2026-10-05T10:00:00Z",
        "no_such_plugin-1.0.0": "2026-08-01T10:00:00Z",
    }
    source = tmp_path / "published.json"
    source.write_text(json.dumps(published), encoding="utf-8")
    target = tmp_path / "indexed.json"

    assert _script().main([str(source), str(target)]) == 0

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "epub_merge-2.3.0": "2026-09-01T10:00:00Z",
        "epub_merge-2.4.0": "2026-10-05T10:00:00Z",
    }
    assert capsys.readouterr().out == "Indexed 2 of 3 release(s); left out: no_such_plugin-1.0.0\n"


def test_a_wrong_argument_count_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert _script().main([]) == 2
    assert capsys.readouterr().err.startswith("usage: ")
