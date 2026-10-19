"""FanFicFare run inside this plugin's own process (``LIB-D25``).

``FanFicFareLibraryGateway`` is the :class:`~fanficfare_source.protocol.FanFicFareGateway` the pull
engine uses. Each call builds a FanFicFare configuration (:mod:`fanficfare_source.fff_support`) from
the plugin's settings, the user's advanced FanFicFare options and the site's stored sign-in, under
the fixed update policy of :data:`UPDATE_OVERRIDES` (``LIB-D27``), asks FanFicFare's adapter for the
story and, for a download, writes the EPUB with FanFicFare's own writer — what FanFicFare's command
line does for ``--update-epub``: a staged EPUB is rebuilt in place, fetching only the chapters it
lacks. A merged book's chapter files are renamed to FanFicFare's naming in the staged copy first
and a one-chapter book's stamped chapter URL is re-keyed (``LIB-D30``); a staged file FanFicFare
still recognises nothing in is left alone (``LIB-D28``); every result carries the counts the engine
verifies (``LIB-D29``). Nothing here raises — a failure is a ``failed`` or ``unrecognised`` result —
and the plugin process is the isolation boundary: the core ends it on a cancel or a timeout.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ebookerr_sdk.epub import EpubDocument, EpubError
from ebookerr_sdk.spi import CircuitOpenError
from fanficfare import adapters, exceptions, writers
from fanficfare.configurable import Configuration
from fanficfare.epubutils import get_update_data

from fanficfare_source.fff_support import (
    apply_sign_in,
    build_configuration,
    can_sign_in,
    captured_stdout,
    config_sections,
    failure_message,
    is_outage,
    masked_text,
    packaged_base_ini,
    quiet_fanficfare_logging,
    settings_options,
)
from fanficfare_source.protocol import (
    UNREADABLE_MESSAGE,
    UNRECOGNISED_MESSAGE,
    DownloadResult,
    MetadataFetch,
    OnChapter,
)

if TYPE_CHECKING:
    from ebookerr_sdk.spi import CircuitGuard, SiteCredential

logger = logging.getLogger(__name__)

# FanFicFare's filename rule, widened to Unicode: replace only what a filesystem refuses or hides
# (TXE-D4). It holds no "%", which FanFicFare's ini parser would interpolate.
UNICODE_SAFEPATTERN = r'(^\.|/\.|[/\\:*?"<>|\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]+)'

UPDATE_OVERRIDES: Mapping[str, str] = MappingProxyType(
    {
        "always_overwrite": "true",
        "never_make_cover": "true",
        "update_check_recent_chapters": "1",
        "update_preserve_deleted_chapters": "false",
        "output_filename_safepattern": UNICODE_SAFEPATTERN,
        "output_filename": "${title}${formatext}",
        "make_directories": "false",
    }
)
"""The fixed policy of every FanFicFare run (``LIB-D27``).

``always_overwrite``: write even though the staged copy is newer than the story's date (the core
stages it with a fresh mtime). ``never_make_cover``: ebookerr manages covers; every former
command-line run set it. ``update_check_recent_chapters``: re-fetch the newest chapter and keep it
only if its text changed. ``update_preserve_deleted_chapters``: off — ebookerr itself keeps the
chapters a site removed (``LIB-D50``), with their own titles.
``output_filename_safepattern``: :data:`UNICODE_SAFEPATTERN`. ``output_filename`` and
``make_directories``: FanFicFare names no folder — the file lands in the work folder as
``<title>.epub`` and ebookerr chooses where a new book goes (``D56``).
"""


class _RunFailedError(Exception):
    """Internal marker reporting a failed run to the breaker."""


_FFF_CHAPTER_NAME = re.compile(r".*/(file|chapter)\d+(_u\d+)?\.x?html$")
"""The archive names FanFicFare's ``get_update_data`` recognises as chapter files."""


def _rekey_merged_chapters(staged: Path) -> int:
    """Rename a merged book's chapter files to FanFicFare's naming (``LIB-D30``).

    Every spine document carrying ``<meta name="chapterurl">`` whose archive name FanFicFare's
    rule does not match is renamed, in spine order, to ``file0001.xhtml``, ``file0002.xhtml``, …
    (``OEBPS/file0001.xhtml`` when the package sits at the archive root), in the staged copy only.
    Documents without a chapter mark are left for the core's chapter carry-over.

    Args:
        staged: The staged EPUB, changed in place when anything is renamed.

    Returns:
        How many files were renamed (0 leaves the file untouched).
    """
    try:
        doc = EpubDocument.open(staged)
        items = {item.id: item for item in doc.opf.manifest()}
        taken = set(doc.member_names())
        opf_dir = doc.opf_dir
        prefix = "" if opf_dir else "OEBPS/"
        renames: list[tuple[str, str]] = []
        for entry in doc.opf.spine():
            item = items.get(entry.idref)
            # Only an XHTML document with a chapter mark whose name FanFicFare cannot read.
            if item is None or item.media_type != "application/xhtml+xml":
                continue
            if _FFF_CHAPTER_NAME.match(doc.member_name(item.href)):
                continue
            if not doc.chapter(item.href).meta("chapterurl"):
                continue
            number = len(renames) + 1
            while f"{opf_dir}/{prefix}file{number:04d}.xhtml".lstrip("/") in taken:
                number += 1
            new_href = f"{prefix}file{number:04d}.xhtml"
            taken.add(f"{opf_dir}/{new_href}".lstrip("/"))
            renames.append((item.id, new_href))
        for item_id, new_href in renames:
            doc.rename_item(item_id, new_href)
        if renames:
            doc.save()
    except EpubError as exc:
        logger.debug(
            "Could not rename the chapter files of %s (%s)", staged.name, type(exc).__name__
        )
        return 0
    if renames:
        logger.info(
            "Renamed %d chapter file(s) of %s so FanFicFare can update the merged book",
            len(renames),
            staged.name,
        )
    return len(renames)


def _rekey_stamped_chapter(
    adapter: Any, site_urls: list[str], before: int, staged_filename: str
) -> None:
    """Give a one-chapter book's stamped chapter the site's first chapter URL (``LIB-D30``).

    Books from ebookerr releases before 2.22.2 can carry the book's own URL stamped onto a
    one-chapter book's chapter (the retired Chapter URL Stamp plugin did that, ``LIB-D38``).
    When the site lists other chapter URLs, FanFicFare would treat that chapter as one the site
    removed and fetch a fresh chapter 1. When the staged
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
    a ``MetadataFetch``: the story's metadata, or the reason there is none. The ``download()``
    method returns a ``DownloadResult`` and never raises (failures are encoded in
    the result).

    The instance receives the plugin's resolved settings (``settings``), a lookup of a
    site's stored sign-in (``credentials``) and an optional circuit breaker (``circuit`` as a
    CircuitGuard). The ``_configuration()`` method builds the story's configuration from the
    settings, the user's advanced FanFicFare options and the stored sign-in, under the fixed
    update overrides. The circuit breaker is consulted via ``_circuit_key()`` to extract
    the host key and label, ``_blocked()`` to check if a call should be refused,
    and ``_record()`` to report an outage as a failure.

    Lifecycle and invariants:
        When the host's circuit is open (``_blocked()`` returns its label), a call is
        refused without making any request: ``fetch_metadata`` returns a
        ``MetadataFetch`` with that reason and ``download`` returns a ``DownloadResult`` with
        status ``"failed"``. A failure
        is recorded only when an outage occurs (via ``_record(ok=False)``). The
        instance retains the settings, the sign-in lookup and the circuit breaker across
        multiple calls and uses them for all subsequent invocations.
    """

    def __init__(
        self,
        settings: Mapping[str, Any],
        *,
        credentials: Callable[[str], SiteCredential | None] | None = None,
        circuit: CircuitGuard | None = None,
    ) -> None:
        """Remember the settings, the sign-in lookup and the breakers; quiet FanFicFare's logging.

        Args:
            settings: The plugin's resolved settings (ctx.settings), read on every call.
            credentials: Returns the stored sign-in of a URL's site (ctx.credentials); None signs
                in nowhere.
            circuit: The app's shared circuit breakers (``SPI 2.21``); ``None`` records nothing.
        """
        self._settings = settings
        self._credentials = credentials
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
        """Build the story's configuration: settings, advanced options, update policy, sign-in.

        The settings are read now, so a change to them takes effect on the next call. The site's
        stored sign-in, when it has one and FanFicFare can sign in on that site (``LIB-D53``), goes
        on top of everything (``C36``); on any other site it is not even looked up.

        Args:
            url: The story address.

        Returns:
            The FanFicFare configuration for *url*.
        """
        config = build_configuration(
            config_sections(url, unknown_site_ok=False),
            fileform="epub",
            base_ini=packaged_base_ini(),
            options=settings_options(self._settings),
            extra_options=str(self._settings.get("extra_options") or ""),
            overrides=UPDATE_OVERRIDES,
        )
        host = (urlsplit(url).hostname or "").lower()
        credential = None
        if self._credentials is not None:
            if can_sign_in(url):
                credential = self._credentials(url)
            else:
                logger.debug("No sign-in asked for %s: FanFicFare cannot sign in there", host)
        apply_sign_in(config, credential, host)
        return config

    def fetch_metadata(self, url: str) -> MetadataFetch:
        """Return the story's metadata without fetching any chapter, or why there is none.

        No file is written (the former ``--meta-only`` run wrote a title-page EPUB).

        Args:
            url: The story address.

        Returns:
            FanFicFare's ``getAllMetadata()`` mapping plus ``zchapterurls``, the site's chapter
            addresses in order (``LIB-D54``), or ``None`` with the user-facing reason; ``gone``
            is set when FanFicFare reports that the story no longer exists (``LIB-D51``).
        """
        label = self._blocked(url)
        if label is not None:
            return MetadataFetch(None, error=f"{label} is not reachable; retrying automatically")
        try:
            with captured_stdout():
                adapter = adapters.getAdapter(self._configuration(url), url)
                metadata = dict(adapter.getStoryMetadataOnly().getAllMetadata())
                metadata["zchapterurls"] = [str(chapter["url"]) for chapter in adapter.chapterUrls]
        except Exception as exc:  # noqa: BLE001 — every failure is a reason (LIB-D25)
            reason = failure_message(exc, url)
            logger.debug("Metadata fetch failed for %s: %s", url, reason)
            if is_outage(exc):
                self._record(url, ok=False)
            return MetadataFetch(
                None, error=reason, gone=isinstance(exc, exceptions.StoryDoesNotExist)
            )
        self._record(url, ok=True)
        return MetadataFetch(meta=metadata)

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
            logger.warning(
                "FanFicFare failed for %s: %s (%s: %s)",
                url,
                message,
                type(exc).__name__,
                masked_text(exc),
            )
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
            if before == 0 and _rekey_merged_chapters(staged):
                update = get_update_data(str(staged))
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

        # make_directories is off (D56), so FanFicFare would not create a stored path's folder
        target.parent.mkdir(parents=True, exist_ok=True)
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
            site_urls=tuple(site_urls),
            written_urls=tuple(str(chapter_url) for chapter_url in (written[7] or {})),
            added=int(getattr(story, "chapter_added_count", 0) or 0),
            updated=int(getattr(story, "chapter_updated_count", 0) or 0),
            errored=int(getattr(story, "chapter_error_count", 0) or 0),
        )
