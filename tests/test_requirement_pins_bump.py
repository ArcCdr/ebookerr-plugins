"""The dependency bot's logic: which PyPI release is due, and how one update rewrites the repo."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 12, tzinfo=UTC)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its module through sys.modules while the class body runs.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PINS = _load("requirement_pins")


def _file(
    days_old: float, *, yanked: bool = False, requires: str | None = ">=3.7"
) -> dict[str, Any]:
    uploaded = (NOW - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return {"upload_time_iso_8601": uploaded, "yanked": yanked, "requires_python": requires}


def _release_json(releases: dict[str, list[dict[str, Any]]], name: str = "FanFicFare") -> dict:
    return {"info": {"name": name}, "releases": releases}


def _eligible(releases: dict[str, list[dict[str, Any]]], python: str = "3.14.0") -> str | None:
    return PINS.eligible_version(
        _release_json(releases), current="4.62.0", now=NOW, cooldown_days=3, python_version=python
    )


def _plugin(root: Path, plugin_id: str, version: str, requirements: list[str]) -> Path:
    folder = root / "plugins" / "source" / plugin_id
    folder.mkdir(parents=True)
    listed = ", ".join(f'"{r}"' for r in requirements)
    (folder / "manifest.toml").write_text(
        f'id = "{plugin_id}"\nname = "Demo"\nversion = "{version}"\nrequirements = [{listed}]\n',
        encoding="utf-8",
    )
    (folder / "CHANGELOG.txt").write_text(
        f"== {version} ==\nFirst release.\n\n== 1.0.0 ==\nOlder.\n", encoding="utf-8"
    )
    return folder


def _repo(tmp_path: Path, dev: list[str]) -> Path:
    listed = "".join(f'    "{r}",\n' for r in dev)
    (tmp_path / "pyproject.toml").write_text(
        f"[dependency-groups]\ndev = [\n{listed}]\n", encoding="utf-8"
    )
    return tmp_path


def test_the_newest_old_enough_final_release_wins() -> None:
    assert _eligible({"4.63.0": [_file(10)], "4.64.0": [_file(1)]}) == "4.63.0"


def test_a_release_inside_the_cooldown_is_not_taken() -> None:
    assert _eligible({"4.63.0": [_file(2)]}) is None


def test_pre_and_dev_releases_are_skipped() -> None:
    assert _eligible({"4.63.0b1": [_file(10)], "4.63.0.dev1": [_file(10)]}) is None


def test_a_yanked_release_is_skipped() -> None:
    assert _eligible({"4.63.0": [_file(10), _file(10, yanked=True)]}) is None


def test_a_release_without_files_is_skipped() -> None:
    assert _eligible({"4.63.0": []}) is None


def test_a_release_for_another_python_is_skipped() -> None:
    assert _eligible({"4.63.0": [_file(10, requires=">=3.15")]}) is None
    assert (
        _eligible({"4.63.0": [_file(10, requires=">=3.15"), _file(10, requires=None)]}) == "4.63.0"
    )


def test_an_older_or_equal_release_is_not_an_update() -> None:
    assert _eligible({"4.61.0": [_file(40)], "4.62.0": [_file(9)]}) is None


def test_plan_lists_one_update_per_package_with_its_plugins(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0", "pymupdf==1.28.2"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    _plugin(root, "b", "2.0.0", ["fanficfare==4.62.0"])
    _plugin(root, "c", "1.3.0", ["pymupdf==1.28.2"])
    documents = {
        "fanficfare": _release_json({"4.63.0": [_file(10)]}),
        "pymupdf": _release_json({"1.28.3": [_file(10)]}, name="pymupdf"),
    }
    updates = PINS.plan(
        root, now=NOW, cooldown_days=3, fetch=documents.__getitem__, python_version="3.14.0"
    )
    assert [(u.name, u.version) for u in updates] == [
        ("fanficfare", "4.63.0"),
        ("pymupdf", "1.28.3"),
    ]
    first = updates[0].as_dict()
    assert first["branch"] == "deps/fanficfare"
    assert first["title"] == "deps: FanFicFare 4.63.0"
    assert first["plugins"] == ["a", "b"]
    assert first["body"] == (
        "Updates FanFicFare from 4.62.0 to 4.63.0 in a, b.\n\n"
        "Release: https://pypi.org/project/fanficfare/4.63.0/\n\n"
        "CI merges this pull request and releases the plugins when the checks pass."
    )


def test_plan_keeps_only_the_named_package(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0", "pymupdf==1.28.2"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    _plugin(root, "c", "1.3.0", ["pymupdf==1.28.2"])
    documents = {
        "fanficfare": _release_json({"4.63.0": [_file(10)]}),
        "pymupdf": _release_json({"1.28.3": [_file(10)]}, name="pymupdf"),
    }
    updates = PINS.plan(
        root,
        now=NOW,
        cooldown_days=3,
        fetch=documents.__getitem__,
        python_version="3.14.0",
        only="PyMuPDF",
    )
    assert [u.name for u in updates] == ["pymupdf"]


def test_plan_without_tracked_pins_is_empty(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare>=4.62.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare>=4.62.0"])
    assert (
        PINS.plan(root, now=NOW, cooldown_days=3, fetch=dict().__getitem__, python_version="3.14.0")
        == []
    )


def test_bump_patch() -> None:
    assert PINS.bump_patch("2.0.1") == "2.0.2"
    with pytest.raises(ValueError, match="X.Y.Z"):
        PINS.bump_patch("1.3")


def test_apply_bumps_a_released_plugin(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["pytest>=8", "fanficfare==4.62.0"])
    folder = _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    changed = PINS.apply(
        root, "fanficfare", "4.63.0", display="FanFicFare", tagged=lambda tag: True
    )
    assert changed == ["a 2.0.2"]
    manifest = (folder / "manifest.toml").read_text(encoding="utf-8")
    assert 'version = "2.0.2"' in manifest
    assert 'requirements = ["fanficfare==4.63.0"]' in manifest
    assert (
        (folder / "CHANGELOG.txt")
        .read_text(encoding="utf-8")
        .startswith("== 2.0.2 ==\nUpdated to FanFicFare 4.63.0.\n\n== 2.0.1 ==")
    )
    assert '"fanficfare==4.63.0",' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_apply_keeps_a_pending_version(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0"])
    folder = _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    changed = PINS.apply(
        root, "fanficfare", "4.63.0", display="FanFicFare", tagged=lambda tag: False
    )
    assert changed == ["a 2.0.1"]
    assert 'version = "2.0.1"' in (folder / "manifest.toml").read_text(encoding="utf-8")
    changelog = (folder / "CHANGELOG.txt").read_text(encoding="utf-8")
    assert changelog.startswith(
        "== 2.0.1 ==\nFirst release. Updated to FanFicFare 4.63.0.\n\n== 1.0.0 =="
    )


def test_apply_twice_changes_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0"])
    folder = _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    PINS.apply(root, "fanficfare", "4.63.0", display="FanFicFare", tagged=lambda tag: True)
    files = [folder / "manifest.toml", folder / "CHANGELOG.txt", root / "pyproject.toml"]
    before = [f.read_bytes() for f in files]
    again = PINS.apply(root, "fanficfare", "4.63.0", display="FanFicFare", tagged=lambda tag: False)
    assert again == []
    assert [f.read_bytes() for f in files] == before


def test_apply_keeps_the_name_as_written(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["FanFicFare==4.62.0"])
    folder = _plugin(root, "a", "2.0.1", ["FanFicFare==4.62.0"])
    PINS.apply(root, "fanficfare", "4.63.0", display="FanFicFare", tagged=lambda tag: True)
    assert '"FanFicFare==4.63.0"' in (folder / "manifest.toml").read_text(encoding="utf-8")
    assert '"FanFicFare==4.63.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_apply_refuses_a_dev_group_without_the_pin(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare>=4.62.0"])
    folder = _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    before = (folder / "manifest.toml").read_bytes()
    with pytest.raises(ValueError, match="no single fanficfare==4.62.0 entry"):
        PINS.apply(root, "fanficfare", "4.63.0", display="FanFicFare", tagged=lambda tag: True)
    assert (folder / "manifest.toml").read_bytes() == before


def test_main_plan_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    document = {
        "info": {"name": "FanFicFare"},
        "releases": {
            "4.63.0": [{"upload_time_iso_8601": old, "yanked": False, "requires_python": None}]
        },
    }
    monkeypatch.setattr(PINS, "fetch_release_json", lambda name: document)
    assert PINS.main(["plan"], root=root) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [entry["version"] for entry in printed] == ["4.63.0"]


def test_main_apply_prints_the_changed_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    monkeypatch.setattr(PINS, "is_tagged", lambda repo, tag: True)
    assert PINS.main(["apply", "fanficfare", "4.63.0", "--display", "FanFicFare"], root=root) == 0
    assert capsys.readouterr().out == "a 2.0.2\n"
