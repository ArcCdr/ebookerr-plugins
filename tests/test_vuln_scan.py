"""The vulnerability scan: report reading, exit-code mapping, trivy and uv calls."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCAN = _load("vuln_scan")

VULN = {
    "PkgName": "soupsieve",
    "InstalledVersion": "3.0.3",
    "FixedVersion": "4.0.0",
    "VulnerabilityID": "CVE-2026-126190",
    "Severity": "HIGH",
}
ONE_VULN = {"Results": [{"Vulnerabilities": [VULN]}]}
EMPTY = {"Results": []}


@dataclass
class Call:
    """One recorded child process call."""

    argv: list[str]
    cwd: Path | None
    env: dict[str, str] | None
    input: str | None


@dataclass
class FakeRun:
    """A stand-in for ``subprocess.run`` that writes the report it was given."""

    db_exit: int = 0
    scan_exit: int = 0
    report: dict[str, Any] = field(default_factory=lambda: EMPTY)
    uv_exit: int = 0
    missing: set[str] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record the call and answer it."""
        self.calls.append(
            Call(list(argv), kwargs.get("cwd"), kwargs.get("env"), kwargs.get("input"))
        )
        if argv[0] in self.missing:
            raise FileNotFoundError(argv[0])
        if argv[0] == "uv":
            return subprocess.CompletedProcess(argv, self.uv_exit, "", "")
        if "--download-db-only" in argv:
            return subprocess.CompletedProcess(argv, self.db_exit, "", "")
        if "--output" in argv:
            if self.scan_exit == 0:
                Path(argv[argv.index("--output") + 1]).write_text(
                    json.dumps(self.report), encoding="utf-8"
                )
            return subprocess.CompletedProcess(argv, self.scan_exit, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def trivy(self) -> list[Call]:
        """Return the trivy calls."""
        return [c for c in self.calls if c.argv[0] == "trivy"]


def _root(tmp_path: Path) -> Path:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    return tmp_path


def _run(fake: FakeRun, *args: str) -> int:
    return SCAN.main(list(args), run=fake, now=lambda: datetime(2026, 11, 17, 4, 41, tzinfo=UTC))


def test_findings_reads_every_vulnerability_of_a_report() -> None:
    other = {"PkgName": "a", "VulnerabilityID": "CVE-1"}
    report = {"Results": [{"Vulnerabilities": [VULN, other, VULN]}, {"Target": "x"}]}
    found = SCAN.findings(report, "uv.lock")
    assert found == sorted(found, key=lambda f: (f.where, f.package, f.advisory))
    assert [f.package for f in found] == ["a", "soupsieve"]
    assert found[1] == SCAN.Finding(
        "uv.lock", "soupsieve", "3.0.3", "4.0.0", "CVE-2026-126190", "HIGH"
    )
    assert {f.where for f in found} == {"uv.lock"}


def test_render_report_is_the_issue_body() -> None:
    found = [
        SCAN.Finding("package-lock.json", "follow-redirects", "1.16.0", "1.16.1", "CVE-2", "LOW"),
        SCAN.Finding("uv.lock", "soupsieve", "3.0.3", "4.0.0", "CVE-2026-126190", "HIGH"),
    ]
    text = SCAN.render_report(
        found, ["uv.lock", "package-lock.json"], datetime(2026, 11, 17, 4, 41, tzinfo=UTC)
    )
    assert text == (
        "## 2 fixable vulnerability finding(s)\n\n"
        "| Where | Package | Installed | Fixed in | Advisory | Severity |\n"
        "|---|---|---|---|---|---|\n"
        "| package-lock.json | follow-redirects | 1.16.0 | 1.16.1 | CVE-2 | LOW |\n"
        "| uv.lock | soupsieve | 3.0.3 | 4.0.0 | CVE-2026-126190 | HIGH |\n\n"
        "Scanned uv.lock, package-lock.json on 2026-11-17 04:41 UTC with Trivy "
        "(`--ignore-unfixed`: an advisory with no fixed release is not listed). "
        "This issue closes itself when a daily scan is clean.\n"
    )


def test_manifest_requirements_is_the_sorted_union(tmp_path: Path) -> None:
    for rel, body in (
        ("plugins/source/a", 'requirements = ["b==1", "a>=2"]'),
        ("plugins/book/c", 'requirements = ["a>=2"]'),
        ("plugins/book/d", 'id = "d"'),
    ):
        folder = tmp_path / rel
        folder.mkdir(parents=True)
        (folder / "manifest.toml").write_text(body + "\n", encoding="utf-8")
    assert SCAN.manifest_requirements(tmp_path) == ["a>=2", "b==1"]


@pytest.mark.parametrize(
    ("db_exit", "scan_exit", "report", "warn_offline", "code"),
    [
        (0, 0, EMPTY, False, 0),
        (0, 0, ONE_VULN, False, 1),
        (0, 1, EMPTY, False, 2),
        (0, 1, EMPTY, True, 2),
        (1, 1, EMPTY, False, 2),
        (1, 1, EMPTY, True, 0),
        (1, 0, EMPTY, False, 0),
    ],
    ids=[
        "clean",
        "finding",
        "scan_error",
        "scan_error_offline_mode",
        "db_down_no_cache",
        "db_down_no_cache_offline_mode",
        "db_down_cached",
    ],
)
def test_main_maps_each_outcome_to_its_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    db_exit: int,
    scan_exit: int,
    report: dict[str, Any],
    warn_offline: bool,
    code: int,
    request: pytest.FixtureRequest,
) -> None:
    fake = FakeRun(db_exit=db_exit, scan_exit=scan_exit, report=report)
    args = ["--root", str(_root(tmp_path)), "uv.lock"]
    if warn_offline:
        args.insert(0, "--warn-offline")
    assert SCAN.main(args, run=fake) == code
    out, err = capsys.readouterr()
    case = request.node.callspec.id
    if case == "finding":
        assert "uv.lock: soupsieve 3.0.3 → 4.0.0 CVE-2026-126190 HIGH" in out
        assert "1 fixable finding(s) in 1 scanned target(s)" in out
    if case == "clean":
        assert "No fixable vulnerability in 1 scanned target(s): uv.lock" in out
    if case.startswith("db_down_no_cache"):
        assert "WARNING: the vulnerability database could not be downloaded" in err
    if case == "db_down_no_cache_offline_mode":
        assert "the push goes on" in err
    if case == "db_down_cached":
        assert "scanned with the cached copy" in err


@pytest.mark.parametrize("warn_offline", [False, True])
def test_a_missing_trivy_never_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], warn_offline: bool
) -> None:
    args = ["--root", str(_root(tmp_path)), "uv.lock"]
    if warn_offline:
        args.insert(0, "--warn-offline")
    assert SCAN.main(args, run=FakeRun(missing={"trivy"})) == 2
    assert "trivy is not installed" in capsys.readouterr().err


def test_every_trivy_call_gets_the_db_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    monkeypatch.delenv("TRIVY_DB_REPOSITORY", raising=False)
    fake = FakeRun()
    _run(fake, "--root", str(root), "uv.lock")
    assert fake.trivy()
    assert all(c.env is not None for c in fake.trivy())
    assert all(
        c.env["TRIVY_DB_REPOSITORY"] == SCAN.DEFAULT_DB_REPOSITORY
        for c in fake.trivy()
        if c.env
    )
    scan = fake.trivy()[-1]
    out = scan.argv[scan.argv.index("--output") + 1]
    assert scan.argv == [
        "trivy", "fs", "--scanners", "vuln", "--include-dev-deps", "--ignore-unfixed",
        "--skip-db-update", "--quiet", "--format", "json", "--output", out, "uv.lock",
    ]  # fmt: skip
    assert scan.cwd == root
    monkeypatch.setenv("TRIVY_DB_REPOSITORY", "x")
    fake = FakeRun()
    _run(fake, "--root", str(root), "uv.lock")
    assert all(c.env and c.env["TRIVY_DB_REPOSITORY"] == "x" for c in fake.trivy())


def test_an_image_is_scanned_at_high_and_critical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = "docker.io/x/y:latest"
    fake = FakeRun(report=ONE_VULN)
    assert _run(fake, "--root", str(tmp_path), "--image", ref) == 1
    argv = fake.trivy()[-1].argv
    assert argv[:7] == [
        "trivy", "image", "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--ignore-unfixed",
    ]
    assert argv[-1] == ref
    assert f"{ref}: soupsieve" in capsys.readouterr().out


def _manifest(root: Path) -> None:
    folder = root / "plugins" / "source" / "a"
    folder.mkdir(parents=True)
    (folder / "manifest.toml").write_text('requirements = ["a>=2"]\n', encoding="utf-8")


def test_the_manifests_resolve_for_the_repository_python(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".python-version").write_text("3.14\n", encoding="utf-8")
    _manifest(tmp_path)
    fake = FakeRun(report=ONE_VULN)
    assert _run(fake, "--root", str(tmp_path), "--manifests") == 1
    uv = next(c for c in fake.calls if c.argv[0] == "uv")
    out = uv.argv[-1]
    assert uv.argv == [
        "uv", "pip", "compile", "-", "--python-version", "3.14",
        "--python-platform", "x86_64-unknown-linux-gnu", "--no-header", "--quiet",
        "--output-file", out,
    ]  # fmt: skip
    assert uv.input is not None
    assert uv.input.splitlines() == ["a>=2"]
    assert fake.trivy()[-1].argv[-1] == out
    assert "manifest requirements: soupsieve" in capsys.readouterr().out


@pytest.mark.parametrize("warn_offline", [False, True])
def test_unresolvable_manifests_warn_only_in_offline_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], warn_offline: bool
) -> None:
    (tmp_path / ".python-version").write_text("3.14\n", encoding="utf-8")
    _manifest(tmp_path)
    _root(tmp_path)
    args = ["--root", str(tmp_path), "--manifests", "uv.lock"]
    if warn_offline:
        args.insert(0, "--warn-offline")
    assert SCAN.main(args, run=FakeRun(uv_exit=2)) == (0 if warn_offline else 2)
    assert "the manifests' requirements could not be resolved" in capsys.readouterr().err


def test_the_report_is_written_on_a_finding_and_removed_when_clean(tmp_path: Path) -> None:
    root = _root(tmp_path)
    report = tmp_path / "out" / "report.md"
    report.parent.mkdir()
    report.write_text("old", encoding="utf-8")
    assert _run(FakeRun(), "--root", str(root), "--report", str(report), "uv.lock") == 0
    assert not report.exists()
    fake = FakeRun(report=ONE_VULN)
    assert _run(fake, "--root", str(root), "--report", str(report), "uv.lock") == 1
    assert report.read_text(encoding="utf-8").startswith(
        "## 1 fixable vulnerability finding(s)\n"
    )
