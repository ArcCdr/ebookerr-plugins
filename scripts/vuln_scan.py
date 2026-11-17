"""Scan the lock files, the plugin manifests' requirements and images for fixable vulnerabilities.

Trivy does the scanning (``--ignore-unfixed``: an advisory with no fixed release is not listed);
``uv`` resolves the manifests' requirements into a pinned list Trivy can read. The script uses the
standard library only, so a runner's system ``python3`` runs it.

Exit codes:

* ``0`` — every target was scanned and none holds a fixable vulnerability (with ``--warn-offline``,
  also: the vulnerability database is unreachable and nothing could be scanned).
* ``1`` — at least one fixable vulnerability.
* ``2`` — the scan did not run (Trivy or uv missing, a scan error, a database that cannot be
  downloaded without a working cached copy). A scan error never passes.

Callers: this repository's CI and pre-push hook, and the core, through
``python -m src.tools.plugins_repo audit|audit-plugins``.

Usage::

    python scripts/vuln_scan.py [--root DIR] [--warn-offline] [--manifests] [--image REF]...
        [--report FILE] [LOCKFILE ...]
    python scripts/vuln_scan.py uv.lock package-lock.json
    python scripts/vuln_scan.py --manifests --report report.md
    python scripts/vuln_scan.py --image docker.io/owner/name:latest
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB_REPOSITORY = "ghcr.io/aquasecurity/trivy-db:2,mirror.gcr.io/aquasec/trivy-db:2"
LOCK_SCAN = ("fs", "--scanners", "vuln", "--include-dev-deps", "--ignore-unfixed")
IMAGE_SCAN = ("image", "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--ignore-unfixed")
EXIT_CLEAN, EXIT_FINDINGS, EXIT_NOT_RUN = 0, 1, 2

MANIFESTS = "manifest requirements"
INSTALL_HINT = "install it (brew install trivy, see docs/RELEASE.md)"

Run = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Finding:
    """One fixable vulnerability of one scanned target."""

    where: str
    package: str
    installed: str
    fixed: str
    advisory: str
    severity: str


@dataclass(frozen=True)
class Target:
    """One thing to scan: a command prefix, what Trivy is pointed at, and the name reported."""

    prefix: tuple[str, ...]
    subject: str
    where: str


class NotRunError(Exception):
    """The scan cannot go on; carries the exit code the run ends with."""

    def __init__(self, message: str, code: int = EXIT_NOT_RUN, *, skippable: bool = False) -> None:
        """Keep the *message* (for stderr), the exit *code* and whether offline mode may go on."""
        super().__init__(message)
        self.code = code
        self.skippable = skippable


def manifest_requirements(root: Path) -> list[str]:
    """Return the sorted, deduplicated union of every manifest's ``requirements`` under *root*.

    Args:
        root: The repository root holding ``plugins/<type>/<id>/manifest.toml``.

    Returns:
        The requirement strings, sorted.
    """
    found: set[str] = set()
    for manifest in sorted(root.glob("plugins/*/*/manifest.toml")):
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        found.update(str(r) for r in data.get("requirements", []))
    return sorted(found)


def findings(report: Mapping[str, Any], where: str) -> list[Finding]:
    """Return the vulnerabilities of one Trivy JSON *report*, deduplicated and sorted.

    Args:
        report: The parsed Trivy report.
        where: The name of the scanned target, kept on every finding.

    Returns:
        The findings sorted by ``(where, package, advisory)``.
    """
    found: set[Finding] = set()
    for result in report.get("Results") or []:
        for vuln in result.get("Vulnerabilities") or []:
            found.add(
                Finding(
                    where,
                    str(vuln.get("PkgName", "")),
                    str(vuln.get("InstalledVersion", "")),
                    str(vuln.get("FixedVersion", "")),
                    str(vuln.get("VulnerabilityID", "")),
                    str(vuln.get("Severity", "")),
                )
            )
    return sorted(found, key=lambda f: (f.where, f.package, f.advisory))


def render_report(found: Sequence[Finding], scanned: Sequence[str], now: datetime) -> str:
    """Return the Markdown report the issue workflow publishes.

    Args:
        found: The findings, in the order to list them.
        scanned: The names of the scanned targets.
        now: The scan time.

    Returns:
        The report text.
    """
    lines = [
        f"## {len(found)} fixable vulnerability finding(s)",
        "",
        "| Where | Package | Installed | Fixed in | Advisory | Severity |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {f.where} | {f.package} | {f.installed} | {f.fixed} | {f.advisory} | {f.severity} |"
        for f in found
    ]
    stamp = now.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    lines += [
        "",
        f"Scanned {', '.join(scanned)} on {stamp} UTC with Trivy (`--ignore-unfixed`: an advisory "
        "with no fixed release is not listed). "
        "This issue closes itself when a daily scan is clean.",
    ]
    return "\n".join(lines) + "\n"


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line; a call with nothing to scan is a usage error."""
    parser = argparse.ArgumentParser(prog="python scripts/vuln_scan.py", description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--warn-offline", action="store_true", help="pre-push exit mapping")
    parser.add_argument("--manifests", action="store_true", help="scan the manifests' requirements")
    parser.add_argument("--image", action="append", default=[], metavar="REF", help="image to scan")
    parser.add_argument("--report", type=Path, help="Markdown report written on a finding")
    parser.add_argument("lockfiles", nargs="*", metavar="LOCKFILE", help="path relative to --root")
    args = parser.parse_args(argv)
    if not (args.lockfiles or args.manifests or args.image):
        parser.error("nothing to scan: give a LOCKFILE, --manifests or --image")
    return args


def _env() -> dict[str, str]:
    """Return the environment of every Trivy call, with the database fallback repositories."""
    env = dict(os.environ)
    env.setdefault("TRIVY_DB_REPOSITORY", DEFAULT_DB_REPOSITORY)
    return env


def _trivy(run: Run, argv: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    """Run one Trivy command from *root*; raise :class:`NotRunError` when Trivy is missing."""
    try:
        return run(
            ["trivy", *argv],
            cwd=root,
            env=_env(),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise NotRunError(f"trivy is not installed: {INSTALL_HINT}") from None


def _resolve_manifests(run: Run, root: Path, tmp: Path, requirements: list[str]) -> Path:
    """Resolve *requirements* into a pinned file under *tmp* and return its path.

    Raises:
        NotRunError: uv is missing, or the resolution fails.
    """
    out = tmp / "manifest-requirements.txt"
    version = (root / ".python-version").read_text(encoding="utf-8").strip()
    argv = [
        "uv", "pip", "compile", "-", "--python-version", version,
        "--python-platform", "x86_64-unknown-linux-gnu", "--no-header", "--quiet",
        "--output-file", str(out),
    ]  # fmt: skip
    try:
        done = run(
            argv, cwd=root, input="\n".join(requirements) + "\n",
            check=False, capture_output=True, text=True,
        )  # fmt: skip
    except FileNotFoundError:
        raise NotRunError(f"uv is not installed: {INSTALL_HINT}") from None
    if done.returncode != 0:
        raise NotRunError(
            f"WARNING: the manifests' requirements could not be resolved (exit {done.returncode}): "
            f"{MANIFESTS} NOT scanned.",
            skippable=True,
        )
    return out


def _read(path: Path) -> Mapping[str, Any] | None:
    """Return the parsed JSON report at *path*, or ``None`` when absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _scan(run: Run, root: Path, tmp: Path, number: int, target: Target) -> tuple[Any, int]:
    """Scan one *target*; return its parsed report (``None`` if it did not run) and Trivy's exit."""
    out = tmp / f"{number}.json"
    argv = [
        *target.prefix, "--skip-db-update", "--quiet", "--format", "json",
        "--output", str(out), target.subject,
    ]  # fmt: skip
    done = _trivy(run, argv, root)
    return (_read(out) if done.returncode == 0 else None), done.returncode


def _manifest_target(
    run: Run, root: Path, tmp: Path, warn_offline: bool
) -> tuple[Target | None, str]:
    """Resolve the manifests into a target; ``(None, notice)`` when there is nothing to scan.

    In offline mode an unresolvable set is warned about on stderr and skipped.
    """
    requirements = manifest_requirements(root)
    if not requirements:
        return None, "No manifest declares a requirement\n"
    try:
        return Target(
            LOCK_SCAN, str(_resolve_manifests(run, root, tmp, requirements)), MANIFESTS
        ), ""
    except NotRunError as error:
        if warn_offline and error.skippable:
            sys.stderr.write(f"{error.args[0]}\n")
            return None, ""
        raise


def _targets(args: argparse.Namespace, run: Run, tmp: Path) -> list[Target]:
    """Build the targets in order: lock files, manifests, images."""
    targets = [Target(LOCK_SCAN, name, name) for name in args.lockfiles]
    if args.manifests:
        target, notice = _manifest_target(run, args.root, tmp, args.warn_offline)
        if target:
            targets.append(target)
        sys.stdout.write(notice)
    targets += [Target(IMAGE_SCAN, ref, ref) for ref in args.image]
    return targets


def _collect(
    args: argparse.Namespace, run: Run, tmp: Path, db_ok: bool
) -> tuple[list[Finding], list[str], bool]:
    """Scan every target; return the findings, the scanned names and whether any was skipped."""
    found: list[Finding] = []
    scanned: list[str] = []
    skipped = False
    for number, target in enumerate(_targets(args, run, tmp)):
        report, code = _scan(run, args.root, tmp, number, target)
        if report is not None:
            scanned.append(target.where)
            found += findings(report, target.where)
            continue
        if db_ok:
            raise NotRunError(f"Trivy could not scan {target.where} (exit {code})")
        sys.stderr.write(
            "WARNING: the vulnerability database could not be downloaded and no cached copy "
            f"works: {target.where} NOT scanned.\n"
        )
        if not args.warn_offline:
            raise NotRunError("")
        sys.stderr.write("WARNING: the push goes on; CI is the gate that enforces.\n")
        skipped = True
    return sorted(set(found), key=lambda f: (f.where, f.package, f.advisory)), scanned, skipped


def _output(found: Sequence[Finding], scanned: Sequence[str]) -> None:
    """Write the findings and the closing line to stdout."""
    for f in found:
        sys.stdout.write(
            f"{f.where}: {f.package} {f.installed} → {f.fixed} {f.advisory} {f.severity}\n"
        )
    if found:
        sys.stdout.write(f"{len(found)} fixable finding(s) in {len(scanned)} scanned target(s)\n")
    else:
        sys.stdout.write(
            f"No fixable vulnerability in {len(scanned)} scanned target(s): {', '.join(scanned)}\n"
        )


def _write_report(
    path: Path | None, found: Sequence[Finding], scanned: Sequence[str], at: datetime
) -> None:
    """Write the report on a finding; remove a stale one when clean."""
    if path is None:
        return
    if found:
        path.write_text(render_report(found, scanned, at), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Run = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Scan the requested targets and return the exit code.

    Args:
        argv: The command line (default: ``sys.argv[1:]``).
        run: ``subprocess.run`` or a stand-in.
        now: The clock, for the report.

    Returns:
        ``0`` clean, ``1`` fixable findings, ``2`` the scan did not run.
    """
    args = _parse(argv)
    try:
        with tempfile.TemporaryDirectory() as name:
            db_ok = _trivy(run, ["fs", "--download-db-only", "--quiet"], args.root).returncode == 0
            found, scanned, skipped = _collect(args, run, Path(name), db_ok)
    except NotRunError as error:
        if error.args[0]:
            sys.stderr.write(error.args[0] + "\n")
        return error.code
    if not db_ok and not skipped:
        sys.stderr.write(
            "WARNING: the vulnerability database could not be updated: "
            "scanned with the cached copy.\n"
        )
    _output(found, scanned)
    _write_report(args.report, found, scanned, now())
    return EXIT_FINDINGS if found else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
