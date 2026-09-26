"""Run every check of the plugins repository, the same way everywhere.

This file is the single definition of "the plugins repository is green". The CI workflow runs
it (``.github/workflows/ci.yml``), and so does the ebookerr core (``poe plugins-check`` there),
with its own interpreter so the plugins are checked against the SDK checkout being developed.
Every step runs as ``sys.executable -m <tool>``: whichever environment runs this script supplies
the tools and the SDK.

The steps, in order: lint, format, types, then per plugin folder (``plugins/<type>/<id>/``) the
SDK's ``pack check`` and that plugin's own tests, then the repository's own ``tests/`` when there
are any. Every step runs even after a failure, so one run reports every problem; the exit code
is 1 when any step failed.

Usage: ``python scripts/check.py`` (from any directory).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
"""The repository root: the folder holding ``plugins/`` and ``registry/``."""

NO_TESTS_COLLECTED = 5
"""pytest's exit code when a folder holds no tests — not a failure for a plugin's tests/."""


@dataclass(frozen=True)
class Step:
    """One check.

    Attributes:
        label: What the step checks, for the log.
        command: The argument vector, run from the repository root.
        passing: The exit codes that count as a pass.
    """

    label: str
    command: list[str]
    passing: frozenset[int] = frozenset({0})


def plugin_folders(root: Path) -> list[Path]:
    """Return the plugin folders (``plugins/<type>/<id>/`` holding a ``manifest.toml``).

    Args:
        root: The repository root.

    Returns:
        The folders, sorted by path (type, then id).
    """
    return sorted(
        p for p in root.glob("plugins/*/*") if p.is_dir() and (p / "manifest.toml").is_file()
    )


def steps(root: Path, python: str) -> list[Step]:
    """Build the ordered checks for the repository at *root*.

    Tests run with ``-p no:randomly`` so the order is the one CI uses even where the
    pytest-randomly plugin is installed.

    Args:
        root: The repository root.
        python: The interpreter every step runs with.

    Returns:
        The steps, in the order they run.
    """
    plan = [
        Step("lint", [python, "-m", "ruff", "check", "."]),
        Step("format", [python, "-m", "ruff", "format", "--check", "."]),
        Step("types", [python, "-m", "mypy"]),
    ]
    for folder in plugin_folders(root):
        rel = folder.relative_to(root).as_posix()
        plan.append(Step(f"pack check {rel}", [python, "-m", "ebookerr_sdk.pack", "check", rel]))
        if (folder / "tests").is_dir():
            plan.append(
                Step(
                    f"tests {rel}",
                    [python, "-m", "pytest", "-p", "no:randomly", f"{rel}/tests"],
                    frozenset({0, NO_TESTS_COLLECTED}),
                )
            )
    if (root / "tests").is_dir():
        plan.append(
            Step("repository tests", [python, "-m", "pytest", "-p", "no:randomly", "tests"])
        )
    return plan


def run(plan: list[Step], root: Path, *, grouped: bool) -> list[str]:
    """Run every step from *root* and return the labels of the ones that failed.

    Args:
        plan: The steps to run.
        root: The working directory for every step.
        grouped: Wrap each step's output in a GitHub Actions log group.

    Returns:
        The failed steps' labels, in run order.
    """
    failed: list[str] = []
    for step in plan:
        sys.stdout.write(f"::group::{step.label}\n" if grouped else f"== {step.label}\n")
        sys.stdout.flush()
        code = subprocess.run(step.command, cwd=root, check=False).returncode
        if grouped:
            sys.stdout.write("::endgroup::\n")
        if code not in step.passing:
            failed.append(step.label)
            sys.stdout.write(f"FAILED: {step.label} (exit {code})\n")
        sys.stdout.flush()
    return failed


def main() -> int:
    """Run every check and report the failures.

    Returns:
        0 when every step passed, else 1.
    """
    plan = steps(ROOT, sys.executable)
    failed = run(plan, ROOT, grouped=os.environ.get("GITHUB_ACTIONS") == "true")
    if failed:
        sys.stdout.write(f"{len(failed)} of {len(plan)} checks failed: {', '.join(failed)}\n")
        return 1
    sys.stdout.write(f"all {len(plan)} checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
