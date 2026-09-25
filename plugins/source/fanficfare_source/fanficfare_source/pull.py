"""FanFicFare pull engine (the FFF-specific half of a Source pull).

``FanFicFarePull.pull`` is ``FanFicFareSourcePlugin.pull()``'s body: metadata poll (state
cache, else fetch) -> no-new-content skip against the staged prior EPUB -> FanFicFare
download -> verify the staged file was freshly written -> derive the canonical book fields
from the downloaded JSON. It is JSON-pure (DEC-31) and database-free (D29): it holds no
repository, store, lock, notifier, cover store or settings collaborator, and returns a
complete ``BookPatch`` for the core's generic ``apply_book_patch`` path to write — locking,
staging-dir lifecycle, persistence, post-process, publish, cover, baseline and notification
are the ``SourcePullService`` core orchestrator's job, not this module's. The FanFicFare
gateway composition (the CLI command, its flags, and stdout/JSON parsing) stays out of this
module too — see ``src/gateways/fanficfare_cli.py`` and ``src/gateways/fanficfare_parser.py``.

``check_for_update`` shares the meta-poll/skip-check logic without downloading, reused by the
Auto-Pull scan so FanFicFare is not polled twice. Both methods cache the metadata poll in the
plugin state a core-provided ``PluginContext`` exposes (``ctx.state_get``/``state_set``, SPI
2.30) rather than in an instance dict, so the cache survives across calls sharing one ``ctx``
and is refused gracefully (logged, never raised) when the core rejects the value.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ebookerr_sdk.domain.dates import format_datetime, parse_datetime
from ebookerr_sdk.domain.metadata import sanitize
from ebookerr_sdk.download.paths import safe_component
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    ChapterLink,
    PluginContext,
    SourcePullError,
    UpdateCheck,
)

from fanficfare_source.cli import pinned_output_template
from fanficfare_source.metadata import (
    chapter_links_from_fanficfare,
    fanficfare_book_id,
    fanficfare_json_to_book_fields,
)
from fanficfare_source.protocol import FanFicFareGateway

logger = logging.getLogger(__name__)

__all__ = ["FanFicFarePull"]

VERIFY_WINDOW_SECONDS = 120.0
_META_CACHE_TTL_S = 900

DEFAULT_AVG_SECONDS = 4.0
EMA_ALPHA = 0.3
MIN_ESTIMATE_SECONDS = 10.0
FFF_PROGRESS_START = 5.0
FFF_PROGRESS_END = 92.0
_TICK_LINEAR_FRACTION = 0.9


class FanFicFarePull:
    """The FanFicFare Source's pull (``pull``) and update-check (``check_for_update``) engine.

    Delegated to by ``FanFicFareSourcePlugin`` (``src/plugins/fanficfare_source.py``,
    manifest ``id="fanficfare_source"``, ``priority=1000``) — the catch-all floor Source
    that claims any ``http(s)`` URL no more specific Source claims first. Database-free
    (D29): every method takes the core's ``prior`` :class:`~ebookerr_sdk.spi.BookView`
    snapshot (or ``None`` for a first pull) instead of reading a repository, and returns a
    declarative result (``UpdateCheck``/``BookPatch``) instead of writing one. The
    no-new-content skip is checked against the staged copy of the prior EPUB the core places
    in ``work_dir`` before calling ``pull``, so a deleted library file self-heals on the next
    pull or Auto-Pull scan.
    """

    def __init__(
        self,
        fanficfare: FanFicFareGateway,
        *,
        verify_window_s: float = VERIFY_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire this engine's one real collaborator: the FanFicFare gateway.

        Args:
            fanficfare: Gateway that runs the FanFicFare CLI (metadata polls and
                downloads).
            verify_window_s: Freshness window (seconds) for the post-download EPUB
                verification check.
            clock: Monotonic clock; overridable for tests.
        """
        self._fanficfare = fanficfare
        self._verify_window_s = verify_window_s
        self._clock = clock

    def _meta_for(
        self, url: str, ctx: PluginContext | None, cancel_event: threading.Event | None
    ) -> dict[str, Any] | None:
        """Return FanFicFare's metadata for ``url``, from ``ctx`` state when cached there.

        With no ``ctx`` (an engine-only caller), always fetches fresh — there is nowhere to
        cache to. A freshly fetched result is cached into ``ctx`` state for 900 seconds;
        the core refusing the write (``ValueError``, e.g. an oversized value) is logged and
        otherwise ignored, never raised.

        Args:
            url: The story/section URL to poll.
            ctx: Runtime services carrying the plugin state cache, or ``None``.
            cancel_event: Propagated to the metadata fetch.

        Returns:
            The metadata payload, or ``None`` on a fetch failure.
        """
        cache_key = f"meta:{url}"
        if ctx is not None:
            cached = ctx.state_get(cache_key)
            if cached is not None:
                return cached  # type: ignore[no-any-return]
        meta = self._fanficfare.fetch_metadata(url, cancel_event=cancel_event)
        if meta is not None and ctx is not None:
            try:
                ctx.state_set(cache_key, meta, ttl_s=_META_CACHE_TTL_S)
            except ValueError as exc:
                logger.debug("FanFicFare metadata for %s not cached: %s", url, exc)
        return meta

    @staticmethod
    def _remote_chapters(meta: dict[str, Any]) -> int | None:
        """Parse the FanFicFare JSON ``numChapters`` field as an int; ``None`` when unparsable."""
        try:
            return int(meta.get("numChapters"))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _no_new_content(prior: BookView, meta: dict[str, Any]) -> bool:
        """Check (a): the authoritative no-new-content skip.

        True when the remote has no more chapters than ``prior`` and ``dateUpdated`` is
        unchanged — i.e. nothing to download. The remote ``dateUpdated`` string is parsed
        before comparison, so the check is shape-independent.
        """
        remote_chapters = FanFicFarePull._remote_chapters(meta)
        return (
            remote_chapters is not None
            and prior.num_chapters is not None
            and remote_chapters <= prior.num_chapters
            and parse_datetime(meta.get("dateUpdated")) == prior.date_updated
        )

    def check_for_update(
        self,
        url: str,
        *,
        prior: BookView | None,
        ctx: PluginContext | None = None,
        cancel_event: threading.Event | None = None,
    ) -> UpdateCheck:
        """Cheap "does this need a re-pull?" probe, reused by the F4 Auto-Pull scan.

        The canonical book id FanFicFare's own metadata names is always reported — an alias
        URL with no row of its own resolves to the canonical book this way, since the core's
        ``_existing_via_alias`` looks the id up (EXP-074). Shares the meta-poll and
        no-new-content check with :meth:`pull`, without downloading, so the Auto-Pull scan
        never polls FanFicFare twice for the same URL. Fail-closed: when the metadata fetch
        itself errors, returns ``needs_update=False`` with ``error`` set rather than guessing.

        Args:
            url: The story/section URL to check.
            prior: The book's current snapshot, or ``None`` when unknown.
            ctx: Runtime services carrying the plugin state cache, or ``None``.
            cancel_event: Propagated to the metadata fetch.

        Returns:
            An ``UpdateCheck``. ``meta`` carries the fetched payload (cached in ``ctx``
            state and reusable by a following :meth:`pull` call); ``book_id`` is the id
            FanFicFare's own metadata derives; ``fields`` carries the observed ``status``.
        """
        meta = self._meta_for(url, ctx, cancel_event)
        if meta is None:
            if prior is not None:
                logger.warning(
                    "Auto-Pull check failed for %s: metadata fetch failed — skipped", url
                )
            return UpdateCheck(
                needs_update=False,
                meta=None,
                book_id=prior.book_id if prior else None,
                error="metadata fetch failed",
            )
        book_id = fanficfare_book_id(meta)
        status = sanitize(meta.get("status"))
        fields: dict[str, Any] = {"status": status} if status else {}
        needs_update = prior is None or not self._no_new_content(prior, meta)
        return UpdateCheck(
            needs_update=needs_update, meta=meta, book_id=book_id, error=None, fields=fields
        )

    def pull(  # noqa: C901
        self,
        url: str,
        work_dir: Path,
        prior: BookView | None,
        ctx: PluginContext,
    ) -> BookPatch:
        """Pull ``url`` into the core-provided ``work_dir``; database-free (D29).

        Meta poll (state cache, else fetch) -> no-new-content skip (check a,
        ``_no_new_content``) against the staged copy of ``prior``'s EPUB the core placed in
        ``work_dir`` before this call, suppressed when that staged copy is missing so a
        deleted library file self-heals -> pin the existing path -> run FanFicFare -> verify
        the output file is fresh (within ``verify_window_s``) -> derive the complete
        ``BookPatch`` from the downloaded JSON -> EMA calibration into ``ctx`` state. It is
        JSON-pure (DEC-31) — it never opens the packaged EPUB to count chapters; the core
        recomputes the packaged count after its EpubEditSession finalizes (FR-TYPE-6a). The
        core owns locking, staging-dir lifecycle, persistence, post-process, publish, cover,
        baseline and notification; those are NOT done here.

        Args:
            url: The story/section URL to pull.
            work_dir: Private staging directory the core orchestrator created for this
                pull, with the prior EPUB already staged into it when one exists (D29);
                FanFicFare downloads here, never into the library.
            prior: The book's current :class:`~ebookerr_sdk.spi.BookView` snapshot, or
                ``None`` for a brand-new book.
            ctx: Runtime services for this call — progress, cancellation, and the plugin
                state cache.

        Returns:
            A complete ``BookPatch``: ``upsert=False`` for a touched-but-unchanged pull
            (the core treats it as *not a change*, FR-TYPE-6); ``upsert=True`` after a
            fresh download, with every derived field, chapter and default the core needs
            to create or update the row.

        Raises:
            SourcePullError: The FanFicFare download failed, the reported output file
                is missing or not freshly written, or no story URL could be derived to
                persist the row.
        """
        ctx.report(2.0)

        meta = self._meta_for(url, ctx, getattr(ctx, "cancel_event", None))
        ctx.check_cancelled()
        ctx.report(5.0)

        remote_chapters: int | None = None
        if prior is not None and meta is not None:
            remote_chapters = self._remote_chapters(meta)
            if self._no_new_content(prior, meta):
                staged_existing = (
                    work_dir / prior.output_filename if prior.output_filename else None
                )
                if staged_existing is not None and staged_existing.is_file():
                    logger.info(
                        "Skipping pull for %s: remote has %s chapter(s), dateUpdated %s"
                        " — no new content",
                        url,
                        remote_chapters,
                        format_datetime(parse_datetime(meta.get("dateUpdated"))),
                    )
                    ctx.report(100.0)
                    return BookPatch(book_id=prior.book_id, fields={}, upsert=False)
                logger.info(
                    "Re-downloading %s despite no new content: no EPUB on disk at %r",
                    url,
                    prior.output_filename or "(unset)",
                )

        # Pin the existing book's path so FanFicFare doesn't rename it on re-download.
        pinned = (
            pinned_output_template(prior.output_filename)
            if prior is not None and prior.output_filename
            else None
        )
        if prior is not None and prior.output_filename and pinned is None:
            logger.debug(
                "FanFicFare output path not pinned (contains '='): %r", prior.output_filename
            )

        on_progress_cb, avg = self._build_ticker(ctx, prior, remote_chapters)

        t0 = self._clock()
        result = self._fanficfare.download(
            url,
            work_dir=work_dir,
            cancel_event=getattr(ctx, "cancel_event", None),
            on_progress=on_progress_cb,
            pinned_output=pinned,
        )
        fff_elapsed = self._clock() - t0
        logger.debug("FanFicFare finished for %s in %.1fs (ok=%s)", url, fff_elapsed, result.ok)

        if not result.ok:
            logger.debug("FanFicFare reported a failure for %s: %s", url, result.error)
            raise SourcePullError(result.error or "download failed")

        ctx.check_cancelled()
        ctx.report(92.0)

        filename = result.output_filename
        if not filename:
            logger.error("EPUB not verified for %s (expected fresh file %r)", url, filename)
            raise SourcePullError(f"EPUB not verified: {filename}")
        staged_path = work_dir / filename
        if (
            not staged_path.is_file()
            or (time.time() - staged_path.stat().st_mtime) > self._verify_window_s
        ):
            logger.error("EPUB not verified for %s (expected fresh file %r)", url, filename)
            raise SourcePullError(f"EPUB not verified: {filename}")

        ctx.report(95.0)

        # For new books, normalize decomposed Unicode in the filename to NFC composed form.
        # This happens before the patch is built so the returned output_filename has the
        # canonical form and any future pin will work correctly.
        if prior is None:
            result_filename_before = filename
            normalised = "/".join(safe_component(part) for part in filename.split("/"))
            if normalised != filename:
                src_path = work_dir / filename
                dst_path = work_dir / normalised
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                src_path.rename(dst_path)
                result = dataclasses.replace(
                    result,
                    output_filename=normalised,
                    json_data={**result.json_data, "output_filename": normalised},
                )
                filename = normalised
                logger.debug(
                    "FanFicFare filename normalised: %r -> %r", result_filename_before, normalised
                )

        json_data = result.json_data
        book_id = fanficfare_book_id(json_data)
        if book_id is None:
            logger.error("Could not persist book for %s (no story URL in metadata)", url)
            raise SourcePullError("could not persist book (no story URL)")

        ctx.report(97.0)

        now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
        fields: dict[str, Any] = {
            **fanficfare_json_to_book_fields(json_data),
            "book_update_time": now,
            "output_filename": filename,
            "format": "epub",
        }
        if fields.get("status") == "Completed":
            fields["auto_pull"] = 0
        elif prior is None:
            fields["auto_pull"] = 1
        if prior is None:
            fields["last_check_time"] = now

        chapters = tuple(
            ChapterLink(url=u, title=t, ordinal=o)
            for u, t, o in chapter_links_from_fanficfare(json_data)
        )

        # EMA calibration off the JSON chapter delta — the Source cannot open the EPUB
        # (DEC-31), so it estimates from the metadata count rather than the packaged one.
        self._calibrate_ema(ctx, prior, self._remote_chapters(json_data), fff_elapsed, avg)

        ctx.report(100.0)
        return BookPatch(book_id=book_id, fields=fields, chapters=chapters, upsert=True)

    def _build_ticker(
        self,
        ctx: PluginContext,
        prior: BookView | None,
        remote_chapters: int | None,
    ) -> tuple[Callable[[float], None], float]:
        """Compute avg_chapter_seconds and the on_progress ticker.

        Returns (on_progress_cb, avg): on_progress_cb reports progress linearly to 0.9 of the
        band up to the estimate, then asymptotically toward the band's end as elapsed time grows.
        The ticker is never None; when metadata lacks chapter count, it uses MIN_ESTIMATE_SECONDS
        as the floor estimate. avg is the current EMA value (from ``ctx`` state) for later
        calibration.
        """
        avg_raw = ctx.state_get("avg_chapter_seconds")
        try:
            avg = float(avg_raw) if avg_raw is not None else DEFAULT_AVG_SECONDS
        except (TypeError, ValueError):
            avg = DEFAULT_AVG_SECONDS

        if remote_chapters is None:
            logger.debug(
                "No remote chapter count for the pull estimate; ticking against the %.0fs floor",
                MIN_ESTIMATE_SECONDS,
            )
            _est = MIN_ESTIMATE_SECONDS
        else:
            to_download = max(
                1,
                (remote_chapters - (prior.num_chapters or 0))
                if prior is not None
                else remote_chapters,
            )
            _est = max(MIN_ESTIMATE_SECONDS, to_download * avg)

        def _tick(elapsed: float) -> None:
            """Report progress for one FanFicFare elapsed-seconds tick.

            Linear across ``_TICK_LINEAR_FRACTION`` of the band up to the estimate, then
            asymptotic across the remainder: a download that overruns its estimate keeps
            creeping toward — but never reaches — the band's end, so the bar always shows
            the task is alive instead of freezing at a cap.
            """
            band = FFF_PROGRESS_END - FFF_PROGRESS_START
            if elapsed <= _est:
                fraction = _TICK_LINEAR_FRACTION * (elapsed / _est)
            else:
                overrun = (elapsed - _est) / _est
                fraction = 1.0 - (1.0 - _TICK_LINEAR_FRACTION) * 0.5**overrun
            ctx.report(FFF_PROGRESS_START + band * fraction)

        return _tick, avg

    def _calibrate_ema(
        self,
        ctx: PluginContext,
        prior: BookView | None,
        chapter_count: int | None,
        fff_elapsed: float,
        avg: float,
    ) -> None:
        """Update ``avg_chapter_seconds`` (in ``ctx`` state) via EMA (α=0.3, clamped to [1s, 120s]).

        ``new_avg = α * (fff_elapsed / downloaded) + (1 - α) * avg``, only when at
        least one chapter was downloaded (``chapter_count`` grew versus ``prior``).
        """
        if chapter_count is None or fff_elapsed <= 0:
            return
        downloaded = (
            chapter_count - (prior.num_chapters or 0) if prior is not None else chapter_count
        )
        if downloaded < 1:
            return
        new_avg = min(
            120.0,
            max(1.0, round(EMA_ALPHA * (fff_elapsed / downloaded) + (1 - EMA_ALPHA) * avg, 2)),
        )
        ctx.state_set("avg_chapter_seconds", new_avg)
        logger.debug(
            "avg_chapter_seconds calibrated to %.2fs (%d chapter(s) in %.1fs)",
            new_avg,
            downloaded,
            fff_elapsed,
        )
