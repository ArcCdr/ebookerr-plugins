"""Refresh ``uv.lock`` to the newest transitive versions, without moving a fresh package back.

Dependabot (and uv's freshness rule) never moves transitive packages — the pinned dependency
of a dependency — so a newer patch of ``soupsieve`` can sit in the lock for months. This script
them: it runs ``uv lock --upgrade`` but keeps any package uploaded within the last seven days
(``--days``, default 7) at its locked version, because a package taken inside that window is
usually a security fix that must not be rolled back. The freshness window is recorded with
``--exclude-newer`` and ``--exclude-newer-package``; a plain second ``uv lock`` then drops the
recorded window without moving any version, because a lock that still records the window fails a
plain ``uv lock --check`` (measured, uv 0.12.18). With ``--npm`` it also runs ``npm update
--package-lock-only`` for the JavaScript lockfile.

``python scripts/lock_refresh.py`` runs the refresh; the ``lock-refresh`` workflows of the plugins
repository and the ebookerr core call it.

Usage: ``python scripts/lock_refresh.py`` (from any directory).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
"""The repository root: the folder holding ``uv.lock`` and ``pyproject.toml``."""


def _format(dt: datetime) -> str:
    """Return *dt* as ``YYYY-MM-DDTHH:MM:SSZ`` with its sub-seconds dropped."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _newest_upload(package: Any) -> datetime | None:
    """Return the newest ``upload-time`` of one ``[[package]]`` over its sdist and wheels.

    Args:
        package: One ``[[package]]`` entry of a ``uv.lock``.

    Returns:
        The newest upload time, or None when the entry has no ``upload-time`` (a path or editable).
    """
    times: list[datetime] = []
    sdist = package.get("sdist")
    if isinstance(sdist, dict):
        uploaded = sdist.get("upload-time")
        if isinstance(uploaded, str):
            times.append(datetime.fromisoformat(uploaded))
    for wheel in package.get("wheels", []) or []:
        if isinstance(wheel, dict):
            uploaded = wheel.get("upload-time")
            if isinstance(uploaded, str):
                times.append(datetime.fromisoformat(uploaded))
    return max(times) if times else None


def newest_uploads(lock_text: str) -> dict[str, datetime]:
    """Return each package's newest upload time, keyed by name.

    Only packages with a ``version`` are considered; a package with no ``upload-time`` (a path or
    editable source) is skipped, and a name locked at two versions keeps the newest upload.

    Args:
        lock_text: The text of a ``uv.lock``.

    Returns:
        The newest upload time per package name.
    """
    newest: dict[str, datetime] = {}
    for package in tomllib.loads(lock_text).get("package", []):
        if "version" not in package:
            continue
        upload = _newest_upload(package)
        if upload is None:
            continue
        name = package["name"]
        if name not in newest or upload > newest[name]:
            newest[name] = upload
    return newest


def _versions_of(package: Any) -> list[str]:
    """Return the ``version`` of one ``[[package]]`` (empty when it has none)."""
    version = package.get("version")
    return [version] if isinstance(version, str) else []


def locked_versions(lock_text: str) -> dict[str, str]:
    """Return each package's locked version(s), keyed by name with versions sorted.

    A name locked at two versions keeps both, joined ``", "``.

    Args:
        lock_text: The text of a ``uv.lock``.

    Returns:
        The locked version(s) per package name, in name order.
    """
    versions: dict[str, list[str]] = {}
    for package in tomllib.loads(lock_text).get("package", []):
        name = package.get("name")
        if not isinstance(name, str):
            continue
        for version in _versions_of(package):
            versions.setdefault(name, []).append(version)
    return {name: ", ".join(sorted(versions[name])) for name in sorted(versions)}


def moved(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Return one line per package whose locked versions changed, in name order.

    Args:
        before: The locked versions before the refresh.
        after: The locked versions after the refresh.

    Returns:
        ``"<name> <old> → <new>"`` per changed package; ``(new)`` / ``(removed)`` mark an added or
        dropped name.
    """
    lines: list[str] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name)
        new = after.get(name)
        if old == new:
            continue
        if old is None:
            lines.append(f"{name} (new) → {new}")
        elif new is None:
            lines.append(f"{name} {old} → (removed)")
        else:
            lines.append(f"{name} {old} → {new}")
    return lines


def refresh_commands(
    cutoff: datetime, fresh: Mapping[str, datetime], *, npm: bool
) -> list[list[str]]:
    """Return the commands that refresh the lock, in run order.

    The first records the freshness window with ``--exclude-newer`` and one
    ``--exclude-newer-package`` per fresh package (its upload advanced by one second); the
    plain second ``uv lock`` then drops the window without moving any version.

    Args:
        cutoff: The freshness cutoff (timezone-aware).
        fresh: Each fresh package's newest upload time.
        npm: Also run the npm refresh.

    Returns:
        The commands, each an argument vector.
    """
    exclude: list[str] = []
    for name in sorted(fresh):
        exclude.extend(
            ("--exclude-newer-package", f"{name}={_format(fresh[name] + timedelta(seconds=1))}")
        )
    commands: list[list[str]] = [
        ["uv", "lock", "--upgrade", "--exclude-newer", _format(cutoff), *exclude],
        ["uv", "lock"],
    ]
    if npm:
        commands.append(
            [
                "npm",
                "update",
                "--package-lock-only",
                "--no-audit",
                "--no-fund",
                "--before",
                _format(cutoff),
            ]
        )
    return commands


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse *argv* into the script's arguments.

    Args:
        argv: The arguments after the program name (``sys.argv[1:]`` when None).

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python scripts/lock_refresh.py",
        description="Refresh the lock to the newest transitive versions",
    )
    parser.add_argument(
        "--root", default=ROOT, type=Path, help="The repository root (default: this repository)."
    )
    parser.add_argument(
        "--days", type=int, default=7, help="The freshness window in days (default: 7)."
    )
    parser.add_argument("--npm", action="store_true", help="Also refresh the JavaScript lockfile.")
    return parser.parse_args(argv)


def _read_lock(root: Path) -> str:
    """Return the text of *root*/uv.lock.

    Args:
        root: The repository root.

    Raises:
        FileNotFoundError: The lockfile is absent.
    """
    return (root / "uv.lock").read_text(encoding="utf-8")


def _fresh(uploads: Mapping[str, datetime], cutoff: datetime) -> dict[str, datetime]:
    """Return each package whose newest upload is after *cutoff*, keyed by name.

    Args:
        uploads: Each package's newest upload time.
        cutoff: The freshness cutoff.

    Returns:
        The fresh packages.
    """
    return {name: upload for name, upload in uploads.items() if upload > cutoff}


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Refresh the lock at ``--root`` and report what moved.

    Args:
        argv: The arguments after the program name (``sys.argv[1:]`` when None).
        run: Runs a command as ``run(command, cwd=root, check=False)``; its ``returncode`` decides
            whether the refresh continues.
        now: Returns the current time (timezone-aware).

    Returns:
        0 after the last command, else 1 on a missing lock or a command that exits non-zero.
    """
    args = _parse(argv)
    try:
        lock_text = _read_lock(args.root)
    except FileNotFoundError:
        sys.stderr.write(f"No uv.lock in {args.root}\n")
        return 1
    cutoff = now() - timedelta(days=args.days)
    fresh = _fresh(newest_uploads(lock_text), cutoff)
    before = locked_versions(lock_text)
    for command in refresh_commands(cutoff, fresh, npm=args.npm):
        result = run(command, cwd=args.root, check=False)
        if result.returncode != 0:
            sys.stderr.write(f"{' '.join(command)} failed (exit {result.returncode})\n")
            return 1
    moved_lines = moved(before, locked_versions(_read_lock(args.root)))
    if moved_lines:
        for line in moved_lines:
            sys.stdout.write(f"{line}\n")
        sys.stdout.write(f"{len(moved_lines)} package(s) moved\n")
        return 0
    sys.stdout.write("The lock is already current\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
