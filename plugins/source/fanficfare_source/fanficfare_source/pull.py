"""FanFicFare pull engine (the FFF-specific half of a Source pull).

``FanFicFarePull.pull`` is ``FanFicFareSourcePlugin.pull()``'s body: metadata poll (state
cache, else fetch) -> no-new-content skip against the staged prior EPUB -> FanFicFare
download -> verify the result's counts (``LIB-D29``) and that the staged file was freshly
written -> derive the canonical book fields from the downloaded JSON. It is JSON-pure (DEC-31)
and database-free (D29): it holds no repository, store, lock, notifier, cover store or
settings collaborator, and returns a complete ``BookPatch`` for the core's generic
``apply_book_patch`` path to write — locking, staging-dir lifecycle, persistence,
post-process, publish, cover, baseline and notification are the ``SourcePullService`` core
orchestrator's job, not this module's. The FanFicFare gateway (``fanficfare_source.library``,
``LIB-D25``) is injected; this module never imports FanFicFare.

``check_for_update`` shares the meta-poll/skip-check logic without downloading, reused by the
Auto-Pull scan so FanFicFare is not polled twice. Both methods cache the metadata poll in the
plugin state a core-provided ``PluginContext`` exposes (``ctx.state_get``/``state_set``, SPI
2.30) rather than in an instance dict, so the cache survives across calls sharing one ``ctx``
and is refused gracefully (logged, never raised) when the core rejects the value.
"""

from __future__ import annotations

import dataclasses
import logging
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

from fanficfare_source.metadata import (
    chapter_links_from_fanficfare,
    fanficfare_book_id,
    fanficfare_json_to_book_fields,
)
from fanficfare_source.protocol import DownloadResult, FanFicFareGateway

logger = logging.getLogger(__name__)

__all__ = ["FanFicFarePull"]

VERIFY_WINDOW_SECONDS = 120.0
_META_CACHE_TTL_S = 900

FFF_PROGRESS_START = 5.0
FFF_PROGRESS_END = 92.0

INCONSISTENT_MESSAGE = (
    "FanFicFare's result did not add up ({rule}: the site lists {site} chapter(s), the book had "
    "{before}, {added} were added, the new file holds {after}); nothing was changed"
)
"""The pull's failure when a FanFicFare result breaks a verification rule (``LIB-D29``)."""


class FanFicFarePull:
    """The FanFicFare Source's pull (``pull``) and update-check (``check_for_update``) engine.

    Delegated to by ``fanficfare_source.plugin.FanFicFareSourcePlugin`` (manifest
    ``id="fanficfare_source"``, ``priority=1000``) — the catch-all floor Source
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
            fanficfare: Gateway to FanFicFare (metadata polls and downloads).
            verify_window_s: Freshness window (seconds) for the post-download EPUB
                verification check.
            clock: Monotonic clock; overridable for tests.
        """
        self._fanficfare = fanficfare
        self._verify_window_s = verify_window_s
        self._clock = clock

    def _meta_for(self, url: str, ctx: PluginContext | None) -> dict[str, Any] | None:
        """Return FanFicFare's metadata for ``url``, from ``ctx`` state when cached there.

        With no ``ctx`` (an engine-only caller), always fetches fresh — there is nowhere to
        cache to. A freshly fetched result is cached into ``ctx`` state for 900 seconds;
        the core refusing the write (``ValueError``, e.g. an oversized value) is logged and
        otherwise ignored, never raised.

        Args:
            url: The story/section URL to poll.
            ctx: Runtime services carrying the plugin state cache, or ``None``.

        Returns:
            The metadata payload, or ``None`` on a fetch failure.
        """
        cache_key = f"meta:{url}"
        if ctx is not None:
            cached = ctx.state_get(cache_key)
            if cached is not None:
                return cached  # type: ignore[no-any-return]
        meta = self._fanficfare.fetch_metadata(url)
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

        Returns:
            An ``UpdateCheck``. ``meta`` carries the fetched payload (cached in ``ctx``
            state and reusable by a following :meth:`pull` call); ``book_id`` is the id
            FanFicFare's own metadata derives; ``fields`` carries the observed ``status``.
        """
        meta = self._meta_for(url, ctx)
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
        deleted library file self-heals -> run FanFicFare (an existing book's EPUB is rebuilt
        at its stored path, ``LIB-D27``) -> verify the result's counts (``LIB-D29``) and that
        the output file is fresh (within ``verify_window_s``) -> derive the complete
        ``BookPatch`` from the downloaded JSON. It is JSON-pure (DEC-31) — it never opens the
        packaged EPUB to count chapters; the core recomputes the packaged count after its
        EpubEditSession finalizes (FR-TYPE-6a). The core owns locking, staging-dir lifecycle,
        persistence, post-process, publish, cover, baseline and notification; those are NOT
        done here.

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
            SourcePullError: The FanFicFare download failed, the result's counts failed
                verification (``LIB-D29``), the reported output file is missing or not
                freshly written, or no story URL could be derived to persist the row.
        """
        ctx.report(2.0)

        meta = self._meta_for(url, ctx)
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

        staged_filename = (
            prior.output_filename if prior is not None and prior.output_filename else None
        )
        t0 = self._clock()
        result = self._fanficfare.download(
            url,
            work_dir=work_dir,
            staged_filename=staged_filename,
            on_chapter=lambda done, total: self._report_chapter(ctx, done, total),
        )
        elapsed = self._clock() - t0
        if not result.ok:
            logger.debug("FanFicFare reported a failure for %s: %s", url, result.error)
            raise SourcePullError(result.error or "download failed")
        logger.info(
            "FanFicFare %s %s in %.1fs: the site lists %d chapter(s); the book had %d and "
            "now holds %d (%d added, %d re-fetched)",
            result.outcome,
            url,
            elapsed,
            result.site_chapters,
            result.chapters_before,
            result.chapters_after,
            result.added,
            result.updated,
        )
        if result.errored:
            logger.warning(
                "FanFicFare wrote %d chapter(s) of %s as errors (continue_on_chapter_error is on)",
                result.errored,
                url,
            )
        self._verify(url, result)
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
        # canonical form and a later update is rebuilt at the same path.
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

        ctx.report(100.0)
        return BookPatch(book_id=book_id, fields=fields, chapters=chapters, upsert=True)

    @staticmethod
    def _report_chapter(ctx: PluginContext, done: int, total: int) -> None:
        """Map FanFicFare's chapter progress onto the 5 %–92 % band, noting ``done of total``.

        Args:
            ctx: The pull's runtime services.
            done: Chapters FanFicFare has assembled.
            total: Chapters in the book.
        """
        fraction = done / total if total > 0 else 1.0
        ctx.report(
            FFF_PROGRESS_START + (FFF_PROGRESS_END - FFF_PROGRESS_START) * fraction,
            note=f"{done} of {total} chapters",
        )

    def _verify(self, url: str, result: DownloadResult) -> None:
        """Reject a FanFicFare result whose counts do not add up (``LIB-D29``).

        The rules, checked in this order, the first failing one named: duplicate chapters (the
        written file's distinct chapter URLs differ from its chapter count); an incomplete
        download (a fresh EPUB lacks chapters the site lists); chapters lost or doubled (an
        update's chapter count is not the old count plus the chapters added); chapters the site
        lists are missing (an update holds fewer chapters than the site lists). With
        ``update_preserve_deleted_chapters`` the written book is the old chapters plus the site's
        new ones, so ``after == before + added`` and ``after >= site``; a fresh download holds
        exactly the site's chapters. This detects an update that should have happened and did
        not, without a forced full download.

        Args:
            url: The story address (log only).
            result: FanFicFare's result.

        Raises:
            SourcePullError: A rule failed; :data:`INCONSISTENT_MESSAGE` names the rule and the
                numbers, after one WARNING with every count.
        """
        rule: str | None = None
        if result.distinct_urls_after != result.chapters_after:
            rule = "duplicate chapters"
        elif result.outcome == "created" and result.chapters_after != result.site_chapters:
            rule = "incomplete download"
        elif (
            result.outcome == "updated"
            and result.chapters_after != result.chapters_before + result.added
        ):
            rule = "chapters lost or doubled"
        elif result.outcome == "updated" and result.chapters_after < result.site_chapters:
            rule = "chapters the site lists are missing"
        if rule is None:
            return
        logger.warning(
            "FanFicFare result rejected for %s: %s (site=%d, before=%d, added=%d, re-fetched=%d, "
            "after=%d, distinct URLs=%d)",
            url,
            rule,
            result.site_chapters,
            result.chapters_before,
            result.added,
            result.updated,
            result.chapters_after,
            result.distinct_urls_after,
        )
        raise SourcePullError(
            INCONSISTENT_MESSAGE.format(
                rule=rule,
                site=result.site_chapters,
                before=result.chapters_before,
                added=result.added,
                after=result.chapters_after,
            )
        )
