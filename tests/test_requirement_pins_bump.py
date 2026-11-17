"""How a dependency update rewrites the repo: ``apply``, ``sync`` and the re-lock."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


def test_main_apply_prints_the_changed_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    monkeypatch.setattr(PINS, "is_tagged", lambda repo, tag: True)
    assert PINS.main(["apply", "fanficfare", "4.63.0", "--display", "FanFicFare"], root=root) == 0
    assert capsys.readouterr().out == "a 2.0.2\n"


def test_sync_moves_the_manifests_to_the_dev_pin(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare==4.64.0"])
    folder = _plugin(root, "a", "2.0.1", ["fanficfare==4.63.0"])
    dev_before = (root / "pyproject.toml").read_bytes()
    changed = PINS.sync(root, display=lambda name: "FanFicFare", tagged=lambda tag: True)
    assert changed == ["a 2.0.2"]
    manifest = (folder / "manifest.toml").read_text(encoding="utf-8")
    assert 'version = "2.0.2"' in manifest
    assert 'requirements = ["fanficfare==4.64.0"]' in manifest
    assert (
        (folder / "CHANGELOG.txt")
        .read_text(encoding="utf-8")
        .startswith("== 2.0.2 ==\nUpdated to FanFicFare 4.64.0.\n\n== 2.0.1 ==")
    )
    assert (root / "pyproject.toml").read_bytes() == dev_before


def test_sync_keeps_a_pending_version(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare==4.64.0"])
    folder = _plugin(root, "a", "2.0.1", ["fanficfare==4.63.0"])
    changed = PINS.sync(root, display=lambda name: "FanFicFare", tagged=lambda tag: False)
    assert changed == ["a 2.0.1"]
    assert 'version = "2.0.1"' in (folder / "manifest.toml").read_text(encoding="utf-8")
    assert (
        (folder / "CHANGELOG.txt")
        .read_text(encoding="utf-8")
        .startswith("== 2.0.1 ==\nFirst release. Updated to FanFicFare 4.64.0.\n\n== 1.0.0 ==")
    )


def test_sync_changes_nothing_when_every_pin_agrees(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare==4.63.0"])
    folder = _plugin(root, "a", "2.0.1", ["fanficfare==4.63.0"])
    files = [folder / "manifest.toml", folder / "CHANGELOG.txt", root / "pyproject.toml"]
    before = [(f, f.read_bytes(), f.stat().st_mtime_ns) for f in files]
    assert PINS.sync(root, display=lambda name: "FanFicFare", tagged=lambda tag: True) == []
    assert [(f, f.read_bytes(), f.stat().st_mtime_ns) for f in files] == before


def test_sync_refuses_a_pin_without_an_exact_dev_entry(tmp_path: Path) -> None:
    root = _repo(tmp_path, ["fanficfare>=4.62.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.63.0"])
    with pytest.raises(ValueError, match="no single fanficfare==4.63.0 entry"):
        PINS.sync(root, display=lambda name: "FanFicFare", tagged=lambda tag: True)


def test_main_sync_prints_the_changed_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["fanficfare==4.64.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.63.0"])
    monkeypatch.setattr(PINS, "is_tagged", lambda repo, tag: True)
    monkeypatch.setattr(PINS, "fetch_release_json", lambda name: {"info": {"name": "FanFicFare"}})
    assert PINS.main(["sync"], root=root) == 0
    assert capsys.readouterr().out == "a 2.0.2\n"
    assert PINS.main(["sync"], root=root) == 0
    assert capsys.readouterr().out == "Every tracked pin already matches the dev group\n"


def test_main_apply_relocks_after_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    calls: list[tuple[list[str], dict[str, object], str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs, (root / "pyproject.toml").read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(PINS, "is_tagged", lambda repo, tag: True)
    monkeypatch.setattr(PINS.subprocess, "run", fake_run)
    assert PINS.main(["apply", "fanficfare", "4.63.0", "--display", "FanFicFare"], root=root) == 0
    assert len(calls) == 1
    command, kwargs, pyproject = calls[0]
    assert command == ["uv", "lock"]
    assert kwargs["cwd"] == root
    assert "fanficfare==4.63.0" in pyproject


def test_main_apply_reports_a_failed_relock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["fanficfare==4.62.0"])
    _plugin(root, "a", "2.0.1", ["fanficfare==4.62.0"])
    monkeypatch.setattr(PINS, "is_tagged", lambda repo, tag: True)
    monkeypatch.setattr(
        PINS.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 2)
    )
    assert PINS.main(["apply", "fanficfare", "4.63.0", "--display", "FanFicFare"], root=root) == 1
    assert "uv lock failed (exit 2)" in capsys.readouterr().err
