"""FanFicFare's gateway shape and result type (moved from the core)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Typed result of one FanFicFare invocation (parsing done by the gateway)."""

    ok: bool
    json_data: dict[str, Any]  # validated metadata (author/category/title present)
    output_filename: str | None  # relative to cwd/library
    was_update: bool  # FanFicFare updated an existing file in place
    raw_stdout: str = ""
    raw_stderr: str = ""
    error: str = ""


@runtime_checkable
class FanFicFareGateway(Protocol):
    """Wraps the FanFicFare CLI."""

    def fetch_metadata(
        self,
        url: str,
        *,
        timeout_s: int = 600,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Poll for a story's metadata only, without downloading a book.

        Returns the raw metadata dict, or ``None`` on any error: timeout, OS
        error, cancellation, or output that can't be classified as success.
        """
        ...

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
        """Download or update a story into ``work_dir``.

        Never raises: a timeout, an unspawnable executable, a cancellation, or
        unparseable output all come back as ``DownloadResult(ok=False, ...)``.
        """
        ...

    def is_available(self) -> bool:
        """Return whether the FanFicFare executable can be invoked."""
        ...
