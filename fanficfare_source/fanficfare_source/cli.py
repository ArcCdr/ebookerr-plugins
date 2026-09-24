"""Real :class:`FanFicFareGateway` backed by the FanFicFare CLI.

Runs the exact FanFicFare command (see :meth:`FanFicFareCliGateway.download` and
:meth:`FanFicFareCliGateway.fetch_metadata` for the two command lines) as a
subprocess in the book library directory, then delegates all stdout/stderr
interpretation to the pure :mod:`fanficfare_source.parser`. The subprocess
runner is injected so tests never spawn a real process (``os.system`` is never
used — CLAUDE.md).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ebookerr_sdk.spi import CircuitOpenError

from fanficfare_source.protocol import DownloadResult
from fanficfare_source.parser import (
    classify_error,
    detect_was_update,
    extract_metadata_json,
)

if TYPE_CHECKING:
    from ebookerr_sdk.spi import CircuitGuard

logger = logging.getLogger(__name__)

# FanFicFare's filename rule, widened to Unicode: replace only what a filesystem
# refuses or hides (TXE-D4). Contains no "=" and no "%" — FanFicFare's -o parser
# splits on "=" and its ini parser interpolates "%".
UNICODE_SAFEPATTERN = r'(^\.|/\.|[/\\:*?"<>|\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]+)'

POLL_INTERVAL_SECONDS = 0.25
"""Drives the cancellation poll and the progress ticker together. Chosen at 0.25s so the pull
bar can advance at close to the telemetry bus's own 200ms publish cadence; a shorter
interval would only burn wake-ups against a throttle that would drop the extra samples.
"""


def pinned_output_template(output_filename: str) -> str | None:
    """Return a FanFicFare ``output_filename`` override that preserves a stored library path.

    ``$`` → ``$$`` (string.Template) and ``%`` → ``%%`` (configparser); the
    ``.epub`` suffix becomes ``${formatext}``. ``None`` when the path contains
    ``=``, which FanFicFare's ``-o`` parser cannot carry.

    Args:
        output_filename: A stored library-relative EPUB path (e.g. ``"author/Title.epub"``).

    Returns:
        An escaped template string compatible with FanFicFare's ``-o`` option, or ``None``
        if the path contains ``=``.
    """
    if "=" in output_filename:
        return None
    stem = output_filename[:-5] if output_filename.lower().endswith(".epub") else output_filename
    return stem.replace("$", "$$").replace("%", "%%") + "${formatext}"


Runner = Callable[
    [
        list[str],
        Path,
        int,
        "threading.Event | None",
        "Callable[[float], None] | None",
    ],
    "subprocess.CompletedProcess[str]",
]


class _RunFailedError(Exception):
    """Internal marker reporting a failed run to the breaker."""


def _default_runner(  # pragma: no cover - real subprocess, exercised only in e2e
    argv: list[str],
    cwd: Path,
    timeout: int,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` as a real subprocess, polling for cancellation and progress.

    Pipes are UTF-8; undecodable bytes become U+FFFD rather than raising (``TXE-D9``).

    Args:
        argv: Full command line to execute.
        cwd: Working directory for the subprocess.
        timeout: Overall timeout in seconds.
        cancel_event: Polled every ``POLL_INTERVAL_SECONDS``; when set, the subprocess is
            terminated (killed if it hasn't exited within 5s) and a synthetic
            ``returncode=-1`` result is returned instead of raising.
        on_progress: Called with the elapsed seconds on every poll while the
            subprocess is still running.

    Raises:
        subprocess.TimeoutExpired: If ``timeout`` elapses without the
            subprocess completing or being cancelled.

    Returns:
        The subprocess's completed result, or the synthetic cancelled one.
    """
    with subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    ) as proc:
        start = time.monotonic()
        end = start + timeout
        while time.monotonic() < end:
            try:
                stdout, stderr = proc.communicate(timeout=POLL_INTERVAL_SECONDS)
                return subprocess.CompletedProcess(
                    args=argv, returncode=proc.returncode, stdout=stdout, stderr=stderr
                )
            except subprocess.TimeoutExpired:
                if on_progress is not None:
                    on_progress(time.monotonic() - start)
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return subprocess.CompletedProcess(
                        args=argv, returncode=-1, stdout="", stderr=""
                    )
        proc.kill()
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)


class FanFicFareCliGateway:
    """Invoke FanFicFare and return a typed :class:`DownloadResult`."""

    def __init__(
        self,
        config_ini: Path,
        *,
        executable: str = "fanficfare",
        runner: Runner | None = None,
        circuit: CircuitGuard | None = None,
    ) -> None:
        """Configure the gateway.

        Args:
            config_ini: Path to the FanFicFare personal config passed via
                ``-c`` (e.g. ``DATA_DIR/config/personal.ini``).
            executable: Name or path of the FanFicFare executable to invoke.
            runner: Subprocess runner to use; defaults to
                :func:`_default_runner` (a real subprocess). Tests inject a
                fake so no process is ever spawned.
            circuit: The app's shared circuit breakers (``SPI 2.21``).
                ``None`` keeps the pre-2.18.29 behaviour.
        """
        self._config_ini = config_ini
        self._executable = executable
        self._runner = runner or _default_runner
        self._circuit = circuit

    def _circuit_key(self, url: str) -> tuple[str, str]:
        """Return the ``(key, label)`` for *url*'s host, or ``("", "")`` when it has none.

        Args:
            url: The story URL.

        Returns:
            A tuple of (circuit_key, display_label) for the host, or ("", "") if no host.
        """
        host = urlsplit(url).hostname or ""
        return (f"host:{host}", host) if host else ("", "")

    def _record(self, url: str, *, ok: bool) -> None:
        """Report one subprocess run's outcome against its host's breaker.

        FanFicFare never raises — it returns a failure result — so the outcome is reported
        explicitly rather than inferred from an exception (``EXP-269``).

        Args:
            url: The story URL the run was for.
            ok: Whether the run produced a usable result.
        """
        key, label = self._circuit_key(url)
        if self._circuit is None or not key:
            return
        try:
            with self._circuit.guard(key, label=label):
                if not ok:
                    raise _RunFailedError
        except (CircuitOpenError, _RunFailedError):
            pass

    def _build_argv(
        self, url: str, *, update_in_place: bool, force: bool, pinned_output: str | None = None
    ) -> list[str]:
        """Build the FanFicFare argv for a full download/update run.

        With both flags enabled (:meth:`download`'s default) and no pinned output, this produces::

            fanficfare -f epub -c <config_ini> --json-meta \
                -o output_filename_safepattern=<UNICODE_SAFEPATTERN> \
                --update-epub --force --non-interactive <url>

        When ``pinned_output`` is given, an additional ``-o output_filename=<pinned_output>``
        option follows the safepattern override. ``--update-epub`` + ``--force`` update the
        staged file in place and don't skip stories FanFicFare considers "not recently updated".

        Args:
            url: The story URL to pass to FanFicFare.
            update_in_place: Whether to include ``--update-epub``.
            force: Whether to include ``--force``.
            pinned_output: Optional FanFicFare filename template (e.g. from
                :func:`pinned_output_template`) to force the output path; applied as
                ``-o output_filename=<pinned_output>``.

        Returns:
            The full argv, ``self._executable`` first.
        """
        argv = [self._executable, "-f", "epub", "-c", str(self._config_ini), "--json-meta"]
        argv += ["-o", f"output_filename_safepattern={UNICODE_SAFEPATTERN}"]
        if pinned_output is not None:
            argv += ["-o", f"output_filename={pinned_output}"]
        if update_in_place:
            argv.append("--update-epub")
        if force:
            argv.append("--force")
        argv += ["--non-interactive", url]
        return argv

    def fetch_metadata(
        self,
        url: str,
        *,
        timeout_s: int = 600,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Poll FanFicFare for story metadata only, without downloading a book.

        Runs, in an isolated scratch directory removed afterwards::

            fanficfare -f epub -c <config_ini> --json-meta \
                --meta-only --no-meta-chapters --non-interactive <url>

        A dedicated ``tempfile.mkdtemp(prefix="fff-meta-")`` scratch dir is
        used as cwd (instead of a caller-supplied work dir) and deleted in
        ``finally``, because ``--meta-only`` still writes a phantom EPUB
        (~2.6 KB, titlepage only) at the output path relative to cwd
        (measured against FanFicFare 4.58.1); the phantom file must never
        reach the library or a caller's staging dir.

        Args:
            url: The story URL to poll.
            timeout_s: Subprocess timeout in seconds.
            cancel_event: Checked once after the runner returns (also
                forwarded to the runner itself, see :func:`_default_runner`);
                when set, the call is treated as failed.

        Returns:
            The raw FanFicFare metadata dict (untouched, no coercion), or
            ``None`` on any error: timeout, OS error, cancellation, or output
            the parser can't classify as success (unparseable/error response,
            missing required metadata keys).
        """
        key, label = self._circuit_key(url)
        if self._circuit is not None and key and self._circuit.is_open(key):
            logger.info("FanFicFare skipped: %s is in a failure back-off (url=%s)", label, url)
            return None

        scratch = Path(tempfile.mkdtemp(prefix="fff-meta-"))
        try:
            argv = [
                self._executable,
                "-f",
                "epub",
                "-c",
                str(self._config_ini),
                "--json-meta",
                "--meta-only",
                "--no-meta-chapters",
                "--non-interactive",
                url,
            ]
            logger.debug("Fetching metadata (meta-only): %s", url)
            try:
                proc = self._runner(argv, scratch, timeout_s, cancel_event, None)
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.debug("Metadata fetch failed for %s: %s", url, exc)
                self._record(url, ok=False)
                return None
            if cancel_event is not None and cancel_event.is_set():
                logger.debug("Metadata fetch failed for %s: cancelled", url)
                return None
            stdout, stderr = proc.stdout or "", proc.stderr or ""
            error = classify_error(stdout, stderr, proc.returncode)
            metadata = extract_metadata_json(stdout)
            if error is not None or metadata is None:
                logger.debug("Metadata fetch failed for %s: %s", url, error or "no metadata")
                return None
            self._record(url, ok=True)
            return metadata
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def download(
        self,
        url: str,
        *,
        work_dir: Path,
        update_in_place: bool = True,
        force: bool = True,
        timeout_s: int = 3600,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[float], None] | None = None,
        pinned_output: str | None = None,
    ) -> DownloadResult:
        """Run FanFicFare in ``work_dir`` and parse its output into a result.

        Invokes the argv built by :meth:`_build_argv` (see its docstring for
        the exact command) as a subprocess with ``cwd=work_dir`` and a
        60-minute default timeout. Never raises: a timeout, an unspawnable
        executable, a cancellation, or output the parser can't make sense of
        all come back as ``DownloadResult(ok=False, ...)``.

        Args:
            url: The story URL to download or update.
            work_dir: Directory FanFicFare runs in; for an in-place update
                the caller must already have staged the existing EPUB there.
            update_in_place: Whether to pass ``--update-epub``.
            force: Whether to pass ``--force``.
            timeout_s: Subprocess timeout in seconds.
            cancel_event: Forwarded to the runner (the default runner polls
                it and terminates the subprocess once set); after the runner
                returns, if the event is set the result is reported as
                cancelled regardless of what the runner produced.
            on_progress: Optional callback forwarded to the runner, invoked
                with elapsed seconds while FanFicFare is still running.
            pinned_output: Optional FanFicFare filename template to force
                the output path for an existing book (e.g. from
                :func:`pinned_output_template`).

        Returns:
            A :class:`DownloadResult`; ``ok`` is False for any failure mode
            (timeout, OS error, cancellation, parse/classify error).
        """
        key, label = self._circuit_key(url)
        if self._circuit is not None and key and self._circuit.is_open(key):
            logger.info("FanFicFare skipped: %s is in a failure back-off (url=%s)", label, url)
            return self._failure(f"{label} is not reachable; retrying automatically")

        argv = self._build_argv(
            url, update_in_place=update_in_place, force=force, pinned_output=pinned_output
        )
        logger.debug(
            "Running FanFicFare: %s (cwd=%s, timeout=%ss)", " ".join(argv), work_dir, timeout_s
        )
        try:
            proc = self._runner(argv, work_dir, timeout_s, cancel_event, on_progress)
        except subprocess.TimeoutExpired:
            logger.error("FanFicFare timed out after %ss for %s", timeout_s, url)
            self._record(url, ok=False)
            return self._failure(f"FanFicFare timed out after {timeout_s}s")
        except OSError as exc:
            logger.error("FanFicFare could not be executed: %s", exc)
            self._record(url, ok=False)
            return self._failure(f"FanFicFare could not be executed: {exc}")

        if cancel_event is not None and cancel_event.is_set():
            return self._failure("cancelled")

        stdout, stderr = proc.stdout or "", proc.stderr or ""
        error = classify_error(stdout, stderr, proc.returncode)
        metadata = extract_metadata_json(stdout)
        logger.debug(
            "FanFicFare exit=%s output_filename=%r error=%r",
            proc.returncode,
            metadata.get("output_filename") if metadata else None,
            error,
        )
        if error is not None or metadata is None:
            return DownloadResult(
                ok=False,
                json_data=metadata or {},
                output_filename=None,
                was_update=False,
                raw_stdout=stdout,
                raw_stderr=stderr,
                error=error or "FanFicFare returned no metadata",
            )

        output_filename = metadata.get("output_filename")
        self._record(url, ok=True)
        return DownloadResult(
            ok=True,
            json_data=metadata,
            output_filename=output_filename,
            was_update=detect_was_update(stdout, output_filename),
            raw_stdout=stdout,
            raw_stderr=stderr,
        )

    def is_available(self) -> bool:
        """Return whether the configured FanFicFare executable is on ``PATH``."""
        return shutil.which(self._executable) is not None

    @staticmethod
    def _failure(message: str) -> DownloadResult:
        """Build a failed, empty :class:`DownloadResult` carrying only ``message``."""
        return DownloadResult(
            ok=False, json_data={}, output_filename=None, was_update=False, error=message
        )
