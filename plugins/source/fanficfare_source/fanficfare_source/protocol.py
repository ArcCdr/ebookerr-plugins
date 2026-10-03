"""FanFicFare as the pull engine sees it: the gateway's shape and one run's result (``LIB-D25``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

DOWNLOAD_OUTCOMES: tuple[str, ...] = ("created", "updated", "unrecognised", "failed")
"""Every :attr:`DownloadResult.outcome`."""

UNRECOGNISED_MESSAGE = (
    "FanFicFare can't update this book's file: it holds no chapter FanFicFare recognises "
    "(for example after a merge, or a file from elsewhere); nothing was changed"
)
"""The pull's failure when the staged EPUB holds no chapter FanFicFare recognises (``LIB-D28``)."""

UNREADABLE_MESSAGE = "FanFicFare can't read this book's file to update it; nothing was changed"
"""The pull's failure when FanFicFare cannot read the staged EPUB at all (``LIB-D28``)."""

OnChapter = Callable[[int, int], None]
"""Called after each chapter FanFicFare assembles.

Args: chapters done, chapters in the book."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """One FanFicFare run, with the counts the pull engine verifies (``LIB-D25``, ``LIB-D29``).

    Attributes:
        outcome: ``created`` (no staged file: a fresh EPUB was written), ``updated`` (the staged
            EPUB was rebuilt in place), ``unrecognised`` (the staged file holds no chapter
            FanFicFare recognises, or cannot be read; nothing was written) or ``failed``.
        json_data: FanFicFare's metadata (its ``getAllMetadata()`` plus ``output_filename`` and
            ``zchapters``); empty unless a file was written.
        output_filename: The written EPUB, relative to the work folder; ``None`` unless written.
        site_chapters: How many chapters the site lists now.
        chapters_before: Chapters FanFicFare recognised in the staged file (0 when created).
        chapters_after: Chapters FanFicFare recognises in the file it wrote, read back from disk.
        distinct_urls_after: Distinct chapter URLs in the file it wrote.
        added: Chapters fetched that the staged file lacked.
        updated: Chapters re-fetched and replaced because their text changed.
        errored: Chapters written as error placeholders (only with ``continue_on_chapter_error``).
        error: The user-facing reason when ``unrecognised`` or ``failed``.
    """

    outcome: str
    json_data: dict[str, Any] = field(default_factory=dict)
    output_filename: str | None = None
    site_chapters: int = 0
    chapters_before: int = 0
    chapters_after: int = 0
    distinct_urls_after: int = 0
    added: int = 0
    updated: int = 0
    errored: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether a file was written (``created`` or ``updated``)."""
        return self.outcome in ("created", "updated")


@runtime_checkable
class FanFicFareGateway(Protocol):
    """FanFicFare, as :class:`~fanficfare_source.pull.FanFicFarePull` uses it (``LIB-D25``)."""

    def fetch_metadata(self, url: str) -> dict[str, Any] | None:
        """Return the story's metadata without fetching any chapter, or ``None`` on any failure."""
        ...

    def download(
        self,
        url: str,
        *,
        work_dir: Path,
        staged_filename: str | None,
        on_chapter: OnChapter | None = None,
    ) -> DownloadResult:
        """Write the story's EPUB in *work_dir*; never raises.

        *staged_filename* is the book's stored path relative to *work_dir* (``None`` for a new
        book): when that file is staged it is rebuilt in place, when it is missing a fresh EPUB is
        written at that path; a new book takes FanFicFare's own file name.
        """
        ...
