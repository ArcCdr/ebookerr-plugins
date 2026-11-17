"""The weekly lock refresh: take transitive updates without moving a fresh package back."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

LOCK = """\
[[package]]
name = "soupsieve"
version = "4.1.1"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "x", hash = "sha256:00", size = 1, upload-time = "2026-11-14T17:23:31.000Z" }
wheels = [
    { url = "x", hash = "sha256:00", size = 1, upload-time = "2026-11-14T17:23:31.500Z" },
]

[[package]]
name = "ebookerr-sdk"
version = "3.11.0"
source = { editable = "sdk" }

[[package]]
name = "numpy"
version = "2.4.6"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "x", hash = "sha256:00", size = 1, upload-time = "2026-06-01T00:00:00Z" },
]

[[package]]
name = "numpy"
version = "2.5.4"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "x", hash = "sha256:00", size = 1, upload-time = "2026-10-01T00:00:00Z" },
]
"""

NOW = datetime(2026, 11, 17, 17, 55, 30, tzinfo=UTC)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LOCK_REFRESH = _load("lock_refresh")


def _run(
    calls: list[tuple[list[str], Path | None]], *, rewrite: bool = False, exit_code: int = 0
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A fake ``run`` recording each command; rewrite the lock once when asked."""
    state = {"done": False}

    def fake_run(
        argv: list[str], *, cwd: Path | None = None, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd))
        if rewrite and not state["done"]:
            state["done"] = True
            (cwd / "uv.lock").write_text(
                LOCK.replace('version = "2.5.4"', 'version = "2.5.5"', 1), encoding="utf-8"
            )
        return subprocess.CompletedProcess(argv, exit_code)

    return fake_run


def test_newest_uploads_reads_sdist_and_wheels() -> None:
    assert LOCK_REFRESH.newest_uploads(LOCK) == {
        "numpy": datetime(2026, 10, 1, tzinfo=UTC),
        "soupsieve": datetime(2026, 11, 14, 17, 23, 31, 500000, tzinfo=UTC),
    }


def test_locked_versions_joins_two_versions() -> None:
    assert LOCK_REFRESH.locked_versions(LOCK) == {
        "ebookerr-sdk": "3.11.0",
        "numpy": "2.4.6, 2.5.4",
        "soupsieve": "4.1.1",
    }


@pytest.mark.parametrize("npm", [False, True])
def test_refresh_commands_keep_a_fresh_package(npm: bool) -> None:
    cutoff = datetime(2026, 11, 10, 17, 55, 30, tzinfo=UTC)
    fresh = {"soupsieve": datetime(2026, 11, 14, 17, 23, 31, 500000, tzinfo=UTC)}
    commands = LOCK_REFRESH.refresh_commands(cutoff, fresh, npm=npm)
    assert commands == [
        [
            "uv",
            "lock",
            "--upgrade",
            "--exclude-newer",
            "2026-11-10T17:55:30Z",
            "--exclude-newer-package",
            "soupsieve=2026-11-14T17:23:32Z",
        ],
        ["uv", "lock"],
    ] + (
        [
            [
                "npm",
                "update",
                "--package-lock-only",
                "--no-audit",
                "--no-fund",
                "--before",
                "2026-11-10T17:55:30Z",
            ]
        ]
        if npm
        else []
    )


def test_moved_lists_changed_added_and_removed() -> None:
    assert LOCK_REFRESH.moved({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "2.1", "d": "4"}) == [
        "b 2 → 2.1",
        "c 3 → (removed)",
        "d (new) → 4",
    ]


def test_main_refreshes_and_reports_what_moved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    calls: list[tuple[list[str], Path | None]] = []
    code = LOCK_REFRESH.main(
        ["--root", str(tmp_path)], run=_run(calls, rewrite=True), now=lambda: NOW
    )
    assert code == 0
    assert len(calls) == 2
    for _argv, cwd in calls:
        assert cwd == tmp_path
    assert calls[0][0][0] == "uv"
    assert "soupsieve=2026-11-14T17:23:32Z" in calls[0][0]
    assert capsys.readouterr().out == "numpy 2.4.6, 2.5.4 → 2.4.6, 2.5.5\n1 package(s) moved\n"


def test_main_reports_a_current_lock(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    calls: list[tuple[list[str], Path | None]] = []
    code = LOCK_REFRESH.main(["--root", str(tmp_path)], run=_run(calls), now=lambda: NOW)
    assert code == 0
    assert len(calls) == 2
    assert capsys.readouterr().out == "The lock is already current\n"


def test_main_stops_at_the_first_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")
    calls: list[tuple[list[str], Path | None]] = []
    code = LOCK_REFRESH.main(
        ["--root", str(tmp_path)], run=_run(calls, exit_code=2), now=lambda: NOW
    )
    assert code == 1
    assert len(calls) == 1
    assert capsys.readouterr().err == (
        "uv lock --upgrade --exclude-newer 2026-11-10T17:55:30Z "
        "--exclude-newer-package soupsieve=2026-11-14T17:23:32Z failed (exit 2)\n"
    )
