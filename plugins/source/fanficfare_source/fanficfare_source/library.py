"""FanFicFare run inside this plugin's own process (``LIB-D25``).

``FanFicFareLibraryGateway`` is the :class:`~fanficfare_source.protocol.FanFicFareGateway` the pull
engine uses. Each call builds a FanFicFare configuration (:mod:`fanficfare_source.fff_support`) with
the fixed update policy of :data:`UPDATE_OVERRIDES` (``LIB-D27``), asks FanFicFare's adapter for the
story and, for a download, writes the EPUB with FanFicFare's own writer — what FanFicFare's command
line does for ``--update-epub``: a staged EPUB is rebuilt in place, fetching only the chapters it
lacks. A staged file FanFicFare does not recognise is left alone (``LIB-D28``); a one-chapter book's
stamped chapter URL is re-keyed first (``LIB-D30``); every result carries the counts the engine
verifies (``LIB-D29``). Nothing here raises — a failure is a ``failed`` or ``unrecognised`` result —
and the plugin process is the isolation boundary: the core ends it on a cancel or a timeout.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ebookerr_sdk.spi import CircuitOpenError
from fanficfare import adapters, writers
from fanficfare.configurable import Configuration
from fanficfare.epubutils import get_update_data

from fanficfare_source.fff_support import (
    build_configuration,
    captured_stdout,
    config_sections,
    failure_message,
    is_outage,
    quiet_fanficfare_logging,
)
from fanficfare_source.protocol import (
    UNREADABLE_MESSAGE,
    UNRECOGNISED_MESSAGE,
    DownloadResult,
    OnChapter,
)

if TYPE_CHECKING:
    from ebookerr_sdk.spi import CircuitGuard

logger = logging.getLogger(__name__)

# FanFicFare's filename rule, widened to Unicode: replace only what a filesystem refuses or hides
# (TXE-D4). It holds no "%", which FanFicFare's ini parser would interpolate.
UNICODE_SAFEPATTERN = r'(^\.|/\.|[/\\:*?"<>|\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]+)'

UPDATE_OVERRIDES: Mapping[str, str] = MappingProxyType(
    {
        "always_overwrite": "true",
        "never_make_cover": "true",
        "update_check_recent_chapters": "1",
        "update_preserve_deleted_chapters": "true",
        "output_filename_safepattern": UNICODE_SAFEPATTERN,
    }
)
"""The fixed policy of every FanFicFare run (``LIB-D27``).

``always_overwrite``: write even though the staged copy is newer than the story's date (the core
stages it with a fresh mtime). ``never_make_cover``: ebookerr manages covers; every former
command-line run set it. ``update_check_recent_chapters``: re-fetch the newest chapter and keep it
only if its text changed. ``update_preserve_deleted_chapters``: keep chapters the site removed.
``output_filename_safepattern``: :data:`UNICODE_SAFEPATTERN`.
"""


class _RunFailedError(Exception):
    """Internal marker reporting a failed run to the breaker."""


def _rekey_stamped_chapter(
    adapter: Any, site_urls: list[str], before: int, staged_filename: str
) -> None:
    """Give a one-chapter book's stamped chapter the site's first chapter URL (``LIB-D30``).

    Books from ebookerr releases before 2.22.2 can carry the book's own URL stamped onto a
    one-chapter book's chapter (the retired Chapter URL Stamp plugin did that, ``LIB-D38``).
    When the site lists other chapter URLs, FanFicFare would keep that chapter as one the site
    removed (``update_preserve_deleted_chapters``) beside a fresh chapter 1. When the staged
    file holds exactly one chapter, keyed by a URL the site does not list, it is the site's
    first chapter.

    Args:
        adapter: The FanFicFare adapter, its old-chapter maps already loaded.
        site_urls: The chapter URLs the site lists, in order.
        before: How many chapters FanFicFare recognised in the staged file.
        staged_filename: The staged file (log only).
    """
    old_map = adapter.oldchaptersmap or {}
    if before != 1 or len(old_map) != 1 or not site_urls:
        return
    (old_url,) = old_map
    if old_url in site_urls:
        return
    adapter.oldchaptersmap = {site_urls[0]: old_map[old_url]}
    if adapter.oldchaptersdata and old_url in adapter.oldchaptersdata:
        adapter.oldchaptersdata = {site_urls[0]: adapter.oldchaptersdata[old_url]}
    logger.debug(
        "Re-keyed the one chapter of %s from %s to %s", staged_filename, old_url, site_urls[0]
    )


class FanFicFareLibraryGateway:
    """FanFicFare run in this plugin's own process (``LIB-D25``).

    This gateway runs FanFicFare inside the plugin's process to fetch metadata
    (``fetch_metadata``) and download a story (``download``), writing the EPUB with
    FanFicFare's own writer via ``_write``. The ``fetch_metadata()`` method returns
    the story's metadata as a dict or ``None`` on any failure. The ``download()``
    method returns a ``DownloadResult`` and never raises (failures are encoded in
    the result).

    The instance receives the FanFicFare configuration file (``personal_ini`` as a
    Path) and an optional circuit breaker (``circuit`` as a CircuitGuard). The
    ``_configuration()`` method builds the story's configuration with fixed update
    overrides. The circuit breaker is consulted via ``_circuit_key()`` to extract
    the host key and label, ``_blocked()`` to check if a call should be refused,
    and ``_record()`` to report an outage as a failure.

    Lifecycle and invariants:
        When the host's circuit is open (``_blocked()`` returns its label), a call is
        refused without making any request: ``fetch_metadata`` returns ``None`` and
        ``download`` returns a ``DownloadResult`` with status ``"failed"``. A failure
        is recorded only when an outage occurs (via ``_record(ok=False)``). The
        instance retains the configuration file path and circuit breaker across
        multiple calls and uses them for all subsequent invocations.
    """

    def __init__(self, personal_ini: Path, *, circuit: CircuitGuard | None = None) -> None:
        """Remember the configuration file and the breakers; quiet FanFicFare's logging.

        Args:
            personal_ini: This install's FanFicFare ``personal.ini``.
            circuit: The app's shared circuit breakers (``SPI 2.21``); ``None`` records nothing.
        """
        self._personal_ini = personal_ini
        self._circuit = circuit
        quiet_fanficfare_logging()

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
        """Report one FanFicFare call's outcome against its host's breaker.

        Only an outage (:func:`~fanficfare_source.fff_support.is_outage`) is reported as a
        failure, and a completed fetch or download as a success; the outcome is reported
        explicitly rather than inferred from an exception (``EXP-269``).

        Args:
            url: The story URL the call was for.
            ok: Whether the call succeeded.
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

    def _blocked(self, url: str) -> str | None:
        """Return the host's label when its breaker refuses a call, else ``None``.

        A refused call is logged at INFO.
        """
        key, label = self._circuit_key(url)
        if self._circuit is not None and key and self._circuit.is_open(key):
            logger.info("FanFicFare skipped: %s is in a failure back-off (url=%s)", label, url)
            return label
        return None

    def _configuration(self, url: str) -> Configuration:
        """Build the story's configuration with :data:`UPDATE_OVERRIDES`."""
        return build_configuration(
            config_sections(url, unknown_site_ok=False),
            self._personal_ini,
            fileform="epub",
            overrides=UPDATE_OVERRIDES,
        )

    def fetch_metadata(self, url: str) -> dict[str, Any] | None:
        """Return the story's metadata without fetching any chapter, or ``None`` on any failure.

        No file is written (the former ``--meta-only`` run wrote a title-page EPUB).

        Args:
            url: The story address.

        Returns:
            FanFicFare's ``getAllMetadata()`` mapping, or ``None``.
        """
        if self._blocked(url) is not None:
            return None
        try:
            with captured_stdout():
                adapter = adapters.getAdapter(self._configuration(url), url)
                metadata = dict(adapter.getStoryMetadataOnly().getAllMetadata())
        except Exception as exc:  # noqa: BLE001 — every failure is None (LIB-D25)
            logger.debug("Metadata fetch failed for %s: %s", url, failure_message(exc, url))
            if is_outage(exc):
                self._record(url, ok=False)
            return None
        self._record(url, ok=True)
        return metadata

    def download(
        self,
        url: str,
        *,
        work_dir: Path,
        staged_filename: str | None,
        on_chapter: OnChapter | None = None,
    ) -> DownloadResult:
        """Write the story's EPUB in *work_dir*; never raises (see the protocol).

        Args:
            url: The story address.
            work_dir: The pull's private staging folder.
            staged_filename: The book's stored path relative to *work_dir*, or ``None``.
            on_chapter: Called after each chapter FanFicFare assembles.

        Returns:
            The run's result.
        """
        label = self._blocked(url)
        if label is not None:
            return DownloadResult(
                "failed", error=f"{label} is not reachable; retrying automatically"
            )
        try:
            with captured_stdout():
                result = self._write(url, work_dir, staged_filename, on_chapter)
        except Exception as exc:  # noqa: BLE001 — every failure is a result (LIB-D25)
            message = failure_message(exc, url)
            logger.debug("FanFicFare failed for %s: %s (%s)", url, message, type(exc).__name__)
            if is_outage(exc):
                self._record(url, ok=False)
            return DownloadResult("failed", error=message)
        if result.ok:
            self._record(url, ok=True)
        return result

    def _write(
        self,
        url: str,
        work_dir: Path,
        staged_filename: str | None,
        on_chapter: OnChapter | None,
    ) -> DownloadResult:
        """Create or rebuild the EPUB; raises whatever FanFicFare raises."""
        config = self._configuration(url)
        adapter = adapters.getAdapter(config, url)
        adapter.getStoryMetadataOnly()
        site_urls = [str(chapter["url"]) for chapter in adapter.chapterUrls]
        staged = work_dir / staged_filename if staged_filename else None
        before = 0
        if staged is not None and staged.is_file():
            try:
                update = get_update_data(str(staged))
            except Exception as exc:  # noqa: BLE001 — any unreadable file is "unrecognised"
                logger.debug(
                    "FanFicFare cannot read %s for an update (%s)",
                    staged_filename,
                    type(exc).__name__,
                )
                return DownloadResult(
                    "unrecognised", site_chapters=len(site_urls), error=UNREADABLE_MESSAGE
                )
            before = int(update[1] or 0)
            if before == 0:
                logger.info(
                    "FanFicFare recognises no chapter in %s; it is left as it is", staged_filename
                )
                return DownloadResult(
                    "unrecognised", site_chapters=len(site_urls), error=UNRECOGNISED_MESSAGE
                )
            (
                _source,
                _count,
                adapter.oldchapters,
                adapter.oldimgs,
                adapter.oldcover,
                adapter.calibrebookmark,
                adapter.logfile,
                adapter.oldchaptersmap,
                adapter.oldchaptersdata,
            ) = update[0:9]
            _rekey_stamped_chapter(adapter, site_urls, before, str(staged_filename))
            outcome = "updated"
        else:
            outcome = "created"
        output_filename = (
            staged_filename or writers.getWriter("epub", config, adapter).getOutputFileName()
        )
        target = work_dir / output_filename

        def _notify(fraction: float, _story_url: str) -> None:
            """Report one assembled chapter as ``(done, total)``."""
            if on_chapter is not None:
                total = int(adapter.story.getChapterCount())
                on_chapter(min(total, round(fraction * total)), total)

        writers.getWriter("epub", config, adapter).writeStory(
            outfilename=str(target), forceOverwrite=True, notification=_notify
        )
        metadata = dict(adapter.getStoryMetadataOnly().getAllMetadata())
        metadata["output_filename"] = output_filename
        metadata["zchapters"] = [
            (index + 1, chapter) for index, chapter in enumerate(adapter.get_chapters())
        ]
        written = get_update_data(str(target))
        story = adapter.story
        return DownloadResult(
            outcome,
            json_data=metadata,
            output_filename=output_filename,
            site_chapters=len(site_urls),
            chapters_before=before,
            chapters_after=int(written[1] or 0),
            distinct_urls_after=len(written[7] or {}),
            added=int(getattr(story, "chapter_added_count", 0) or 0),
            updated=int(getattr(story, "chapter_updated_count", 0) or 0),
            errored=int(getattr(story, "chapter_error_count", 0) or 0),
        )
