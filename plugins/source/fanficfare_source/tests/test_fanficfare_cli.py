"""Tests for the FanFicFare CLI gateway (subprocess injected; no real spawns)."""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from fanficfare_source.cli import FanFicFareCliGateway
from fanficfare_source.protocol import FanFicFareGateway

URL = "https://literotica.com/s/the-12th-key"


def _proc(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_builds_exact_argv_cwd_and_timeout(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    from fanficfare_source.cli import UNICODE_SAFEPATTERN

    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(argv=argv, cwd=cwd, timeout=timeout)
        return _proc(stdout=stdout)

    ini = tmp_path / "personal.ini"
    library = tmp_path / "books"
    gateway = FanFicFareCliGateway(ini, runner=runner)

    result = gateway.download(URL, work_dir=library)

    assert captured["argv"] == [
        "fanficfare",
        "-f",
        "epub",
        "-c",
        str(ini),
        "--json-meta",
        "-o",
        f"output_filename_safepattern={UNICODE_SAFEPATTERN}",
        "--update-epub",
        "--force",
        "--non-interactive",
        URL,
    ]
    assert captured["cwd"] == library
    assert captured["timeout"] == 3600
    assert result.ok is True
    assert result.output_filename == "gabthewriter/The 12th Key.epub"
    assert result.was_update is False
    assert result.json_data["title"] == "The 12th Key"
    assert result.raw_stdout == stdout


def test_update_run_sets_was_update(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    stdout = (fanficfare_fixtures / "the-12th-key.update.stdout").read_text()
    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=lambda *_: _proc(stdout=stdout))
    result = gateway.download(URL, work_dir=tmp_path)
    assert result.ok is True
    assert result.was_update is True


def test_unsupported_url_returns_error(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    stderr = (fanficfare_fixtures / "unsupported-google.stderr").read_text()
    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=lambda *_: _proc(stderr=stderr, rc=1))
    result = gateway.download("https://www.google.com", work_dir=tmp_path)
    assert result.ok is False
    assert "unsupported site" in result.error
    assert result.output_filename is None
    assert result.json_data == {}


def test_timeout_returns_error(tmp_path: Path) -> None:
    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    result = gateway.download(URL, work_dir=tmp_path, timeout_s=7)
    assert result.ok is False
    assert "timed out" in result.error


def test_os_error_returns_error(tmp_path: Path) -> None:
    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("fanficfare missing")

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    result = gateway.download(URL, work_dir=tmp_path)
    assert result.ok is False
    assert "could not be executed" in result.error


def test_zero_exit_without_metadata_is_error(tmp_path: Path) -> None:
    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=lambda *_: _proc(stdout="no json"))
    result = gateway.download(URL, work_dir=tmp_path)
    assert result.ok is False
    assert "no metadata" in result.error.lower()


def test_flags_can_be_disabled(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _proc(stdout=stdout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    gateway.download(URL, work_dir=tmp_path, update_in_place=False, force=False)
    assert "--update-epub" not in captured["argv"]
    assert "--force" not in captured["argv"]


def test_is_available_true_for_existing_executable(tmp_path: Path) -> None:
    gateway = FanFicFareCliGateway(tmp_path / "p.ini", executable="python3")
    assert gateway.is_available() is True


def test_is_available_false_for_missing_executable(tmp_path: Path) -> None:
    gateway = FanFicFareCliGateway(tmp_path / "p.ini", executable="no-such-binary-xyz-123")
    assert gateway.is_available() is False


def test_gateway_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(FanFicFareCliGateway(tmp_path / "p.ini"), FanFicFareGateway)


# ─── cancel_event support ─────────────────────────────────────────────────────


def test_runner_receives_cancel_event(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    """The runner must receive the cancel_event passed to download()."""
    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured["cancel_event"] = cancel_event
        return _proc(stdout=stdout)

    event = threading.Event()
    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    gateway.download(URL, work_dir=tmp_path, cancel_event=event)
    assert captured["cancel_event"] is event


def test_cancel_event_none_by_default(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    """When no cancel_event is passed, the runner receives None."""
    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured["cancel_event"] = cancel_event
        return _proc(stdout=stdout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    gateway.download(URL, work_dir=tmp_path)
    assert captured["cancel_event"] is None


def test_cancel_event_set_returns_cancelled_result(tmp_path: Path) -> None:
    """When cancel_event is set (before or during the run), the gateway returns
    ok=False with error='cancelled' regardless of the runner's output."""
    cancel_event = threading.Event()
    cancel_event.set()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        _cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _proc(stdout="", stderr="", rc=-9)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    result = gateway.download(URL, work_dir=tmp_path, cancel_event=cancel_event)
    assert result.ok is False
    assert "cancelled" in result.error


# ─── on_progress callback support ────────────────────────────────────────────


def test_download_forwards_on_progress_to_runner(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    """download() must forward the on_progress callback as the 5th runner arg."""
    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured["on_progress"] = on_progress
        return _proc(stdout=stdout)

    cb: Callable[[float], None] = lambda elapsed: None  # noqa: E731
    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    gateway.download(URL, work_dir=tmp_path, on_progress=cb)
    assert captured["on_progress"] is cb


def test_download_on_progress_none_by_default(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    """When no on_progress is passed, the runner receives None."""
    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured["on_progress"] = on_progress
        return _proc(stdout=stdout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    gateway.download(URL, work_dir=tmp_path)
    assert captured["on_progress"] is None


# ─── fetch_metadata ───────────────────────────────────────────────────────────


def test_fetch_metadata_argv_and_cwd_isolation(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    """fetch_metadata uses --meta-only / --no-meta-chapters and a scratch cwd
    that is deleted after the call and is different from the config dir."""
    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()
    ini = tmp_path / "personal.ini"

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(argv=argv, cwd=cwd)
        return _proc(stdout=stdout)

    gateway = FanFicFareCliGateway(ini, runner=runner)
    result = gateway.fetch_metadata(URL)

    assert result is not None
    assert "--meta-only" in captured["argv"]
    assert "--no-meta-chapters" in captured["argv"]
    assert "--json-meta" in captured["argv"]
    assert "--update-epub" not in captured["argv"]
    assert "--force" not in captured["argv"]
    cwd = captured["cwd"]
    assert isinstance(cwd, Path)
    assert cwd != ini.parent
    assert not cwd.exists()


def test_fetch_metadata_scratch_dir_cleaned_on_timeout(tmp_path: Path) -> None:
    """Scratch dir is deleted even when the runner raises TimeoutExpired."""
    observed_cwd: list[Path] = []

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        observed_cwd.append(cwd)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    result = gateway.fetch_metadata(URL)

    assert result is None
    assert len(observed_cwd) == 1
    assert not observed_cwd[0].exists()


def test_fetch_metadata_returns_none_on_nonzero_exit(tmp_path: Path) -> None:
    gateway = FanFicFareCliGateway(
        tmp_path / "p.ini",
        runner=lambda *_: _proc(stdout="", rc=1),
    )
    assert gateway.fetch_metadata(URL) is None


def test_fetch_metadata_returns_none_on_unknown_site(
    tmp_path: Path, fanficfare_fixtures: Path
) -> None:
    stderr = (fanficfare_fixtures / "unsupported-google.stderr").read_text()
    gateway = FanFicFareCliGateway(
        tmp_path / "p.ini",
        runner=lambda *_: _proc(stderr=stderr, rc=1),
    )
    assert gateway.fetch_metadata(URL) is None


def test_fetch_metadata_returns_none_on_unparseable_stdout(tmp_path: Path) -> None:
    gateway = FanFicFareCliGateway(
        tmp_path / "p.ini",
        runner=lambda *_: _proc(stdout="not json at all"),
    )
    assert gateway.fetch_metadata(URL) is None


def test_fetch_metadata_returns_dict_on_success(tmp_path: Path, fanficfare_fixtures: Path) -> None:
    """Returns the parsed metadata dict (raw strings — no coercion) on success."""
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()
    gateway = FanFicFareCliGateway(
        tmp_path / "p.ini",
        runner=lambda *_: _proc(stdout=stdout),
    )
    result = gateway.fetch_metadata(URL)
    assert result is not None
    assert result["title"] == "The 12th Key"
    assert result["author"] == "gabthewriter"


def test_fetch_metadata_phantom_file_deleted_with_scratch_dir(
    tmp_path: Path, fanficfare_fixtures: Path
) -> None:
    """A phantom EPUB written by the runner into its cwd is gone after the call."""
    phantom: list[Path] = []
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        epub = cwd / "phantom.epub"
        epub.write_bytes(b"PK\x03\x04")
        phantom.append(epub)
        return _proc(stdout=stdout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    gateway.fetch_metadata(URL)

    assert len(phantom) == 1
    assert not phantom[0].exists()


def test_fetch_metadata_passes_none_for_on_progress(
    tmp_path: Path, fanficfare_fixtures: Path
) -> None:
    """fetch_metadata always passes None as on_progress to the runner."""
    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured["on_progress"] = on_progress
        return _proc(stdout=stdout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    gateway.fetch_metadata(URL)
    assert captured["on_progress"] is None


def test_download_logs_command_at_debug(
    tmp_path: Path, fanficfare_fixtures: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _proc(stdout=stdout)

    gateway = FanFicFareCliGateway(tmp_path / "p.ini", runner=runner)
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.cli"):
        gateway.download(URL, work_dir=tmp_path)

    messages = [r.getMessage() for r in caplog.records]
    assert any("Running FanFicFare:" in m and URL in m for m in messages)


# ─── poll interval constant ───────────────────────────────────────────────────


def test_poll_interval_constant_is_a_quarter_second() -> None:
    """The poll interval constant is exported and set to 0.25s."""
    from fanficfare_source.cli import POLL_INTERVAL_SECONDS

    assert POLL_INTERVAL_SECONDS == 0.25


def test_runner_polls_at_the_constant(tmp_path: Path) -> None:
    """The default runner uses POLL_INTERVAL_SECONDS for communicate() timeouts."""
    recorded_timeouts: list[float] = []

    class FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            recorded_timeouts.append(timeout)
            if len(recorded_timeouts) < 2:
                raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)
            return "", ""

        @property
        def returncode(self) -> int:
            return 0

    import fanficfare_source.cli

    original_popen = subprocess.Popen
    try:
        subprocess.Popen = FakePopen  # type: ignore
        result = fanficfare_source.cli._default_runner(["test"], tmp_path, timeout=10)
    finally:
        subprocess.Popen = original_popen

    assert len(recorded_timeouts) == 2
    assert all(t == 0.25 for t in recorded_timeouts)
    assert result.returncode == 0


def test_runner_still_ticks_progress_on_each_poll(tmp_path: Path) -> None:
    """progress callback is called on each poll timeout."""
    call_count: list[int] = []

    class FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if len(call_count) < 2:
                raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)
            return "", ""

        @property
        def returncode(self) -> int:
            return 0

    def on_progress_cb(elapsed: float) -> None:
        call_count.append(1)

    import fanficfare_source.cli

    original_popen = subprocess.Popen
    try:
        subprocess.Popen = FakePopen  # type: ignore
        result = fanficfare_source.cli._default_runner(
            ["test"], tmp_path, timeout=10, on_progress=on_progress_cb
        )
    finally:
        subprocess.Popen = original_popen

    assert len(call_count) == 2
    assert result.returncode == 0


def test_runner_still_terminates_on_a_set_cancel_event(tmp_path: Path) -> None:
    """When cancel_event is set, process is terminated and returncode=-1."""
    terminate_called = []

    class FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)

        def terminate(self) -> None:
            terminate_called.append(1)

        def wait(self, timeout: int | None = None) -> int:
            return 0

    import fanficfare_source.cli

    original_popen = subprocess.Popen
    try:
        subprocess.Popen = FakePopen  # type: ignore
        cancel_event = threading.Event()
        cancel_event.set()
        result = fanficfare_source.cli._default_runner(
            ["test"], tmp_path, timeout=10, cancel_event=cancel_event
        )
    finally:
        subprocess.Popen = original_popen

    assert len(terminate_called) == 1
    assert result.returncode == -1


def test_runner_still_kills_a_process_that_ignores_terminate(tmp_path: Path) -> None:
    """When a process ignores terminate(), kill() is called."""
    kill_called = []

    class FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)

        def terminate(self) -> None:
            pass

        def wait(self, timeout: int | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)

        def kill(self) -> None:
            kill_called.append(1)

    import fanficfare_source.cli

    original_popen = subprocess.Popen
    try:
        subprocess.Popen = FakePopen  # type: ignore
        cancel_event = threading.Event()
        cancel_event.set()
        result = fanficfare_source.cli._default_runner(
            ["test"], tmp_path, timeout=10, cancel_event=cancel_event
        )
    finally:
        subprocess.Popen = original_popen

    assert len(kill_called) == 1
    assert result.returncode == -1


def test_runner_still_raises_on_the_overall_timeout(tmp_path: Path) -> None:
    """When overall timeout expires, TimeoutExpired is raised and kill() called."""
    kill_called = []

    class FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)

        def kill(self) -> None:
            kill_called.append(1)

    import fanficfare_source.cli

    original_popen = subprocess.Popen
    try:
        subprocess.Popen = FakePopen  # type: ignore
        with pytest.raises(subprocess.TimeoutExpired):
            fanficfare_source.cli._default_runner(["test"], tmp_path, timeout=0)
    finally:
        subprocess.Popen = original_popen

    assert len(kill_called) == 1


def test_no_bare_half_second_literal_remains() -> None:
    """Verify no bare 0.5 timeout literal remains in the file."""
    source_file = Path(__file__).resolve().parent.parent / "fanficfare_source" / "cli.py"
    content = source_file.read_text()
    assert "timeout=0.5" not in content


def test_runner_decodes_fanficfare_output_as_utf8(tmp_path: Path) -> None:
    """_default_runner opens pipes with UTF-8 encoding and sets UTF-8 env vars (TXE-D9)."""
    kwargs_captured: dict[str, object] = {}

    class FakePopen:
        """Capture kwargs and return minimal interface."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs_captured.update(kwargs)

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

        @property
        def returncode(self) -> int:
            return 0

    import fanficfare_source.cli

    original_popen = subprocess.Popen
    try:
        subprocess.Popen = FakePopen  # type: ignore
        fanficfare_source.cli._default_runner(["test"], tmp_path, timeout=10)
    finally:
        subprocess.Popen = original_popen

    assert kwargs_captured.get("encoding") == "utf-8", "encoding should be utf-8"
    assert kwargs_captured.get("errors") == "replace", "errors should be replace"
    env = kwargs_captured.get("env")
    assert isinstance(env, dict), "env should be a dict"
    assert env.get("PYTHONUTF8") == "1", "PYTHONUTF8 should be 1"
    assert env.get("PYTHONIOENCODING") == "utf-8", "PYTHONIOENCODING should be utf-8"


def test_a_pinned_output_is_passed_as_an_override(
    tmp_path: Path, fanficfare_fixtures: Path
) -> None:
    """When pinned_output is given, it appears in argv as an -o option after safepattern."""
    from fanficfare_source.cli import UNICODE_SAFEPATTERN

    captured: dict[str, object] = {}
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()

    def runner(
        argv: list[str],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _proc(stdout=stdout)

    ini = tmp_path / "personal.ini"
    gateway = FanFicFareCliGateway(ini, runner=runner)
    gateway.download(URL, work_dir=tmp_path, pinned_output="A/B${formatext}")

    argv = captured["argv"]
    assert isinstance(argv, list)
    safepattern_idx = argv.index(f"output_filename_safepattern={UNICODE_SAFEPATTERN}")
    assert argv[safepattern_idx - 1] == "-o"
    assert argv[safepattern_idx + 1] == "-o"
    assert argv[safepattern_idx + 2] == "output_filename=A/B${formatext}"


def test_the_unicode_safepattern_keeps_letters_and_replaces_only_unsafe_characters() -> None:
    """The UNICODE_SAFEPATTERN preserves Unicode letters but replaces unsafe chars."""
    import re

    from fanficfare_source.cli import UNICODE_SAFEPATTERN

    # Unchanged: Unicode letters/accents, numbers, spaces, hyphens, apostrophes, etc.
    result = re.sub(UNICODE_SAFEPATTERN, "_", "Stevo's Café — Война и мир")
    assert result == "Stevo's Café — Война и мир"

    # Replaced: unsafe filesystem characters
    result = re.sub(UNICODE_SAFEPATTERN, "_", 'a/b:c*d?e"f<g>h|i\x01j‮k')
    assert result == "a_b_c_d_e_f_g_h_i_j_k"

    # Pattern must not contain "=" or "%"
    assert "=" not in UNICODE_SAFEPATTERN
    assert "%" not in UNICODE_SAFEPATTERN


def test_pinned_output_template_escapes_and_refuses() -> None:
    """pinned_output_template escapes $ and % but refuses = and removes .epub."""
    from fanficfare_source.cli import pinned_output_template

    # Basic case: escapes nothing, removes .epub, adds ${formatext}
    result = pinned_output_template("Tefler/Three Square Meals - Chapter 181.epub")
    assert result == "Tefler/Three Square Meals - Chapter 181${formatext}"

    # Escapes $ and %
    result = pinned_output_template("A/100% $ok.epub")
    assert result == "A/100%% $$ok${formatext}"

    # Refuses = (returns None)
    result = pinned_output_template("A/x=y.epub")
    assert result is None

    # Works without .epub suffix
    result = pinned_output_template("A/Book Title")
    assert result == "A/Book Title${formatext}"


def test_download_result_defaults() -> None:
    """DownloadResult has sensible defaults for output fields."""
    from fanficfare_source.protocol import DownloadResult

    r = DownloadResult(ok=True, json_data={}, output_filename="a/b.epub", was_update=False)
    assert r.raw_stdout == ""
    assert r.raw_stderr == ""
    assert r.error == ""


def test_download_result_is_frozen() -> None:
    """DownloadResult is immutable once created."""
    import dataclasses

    from fanficfare_source.protocol import DownloadResult

    r = DownloadResult(ok=True, json_data={}, output_filename=None, was_update=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]
