"""Kavita sync business rules (see ARCHITECTURE.md §2.2 for the cross-provider contract).

Kavita is a read-server for manga/EPUBs; each book maps to one Kavita "chapter" inside
one "series" behind the ``KavitaClient`` protocol (:mod:`src.gateways.protocols`).
Unlike Komga, Kavita is not a metadata sink: local/FanFicFare fields are never pushed to
it, and the only series-level action is rating cross-sync.
A file Kavita has indexed but cannot place in a series is reported as unresolved, never
as not found (``EXP-190``).

The sync, for one freshly-downloaded book (:meth:`KavitaService.sync`):

1. Trigger ``scan_folder`` on the book's parent directory the first time it is synced
   (no stored ``external_item_id`` yet) or whenever the file changed since the last link
   attempt (``EXP-202``); either way then find its chapter in Kavita by
   ``output_filename`` — a miss returns a not-``ok`` result, there is no scan-and-poll
   retry loop for *discovery* here, unlike Komga's. A file-changed nudge is different: Kavita
   re-indexes a changed file under a **new** chapter id (``EXP-193``), so the sync waits for
   that re-index (:meth:`KavitaService._wait_for_reindex`, ``CHC-D12``) before trusting
   anything it reports about the file's chapters; a wait that exhausts its poll budget marks
   the sync stale (step 3) rather than blocking it.
2. Push the local rating via ``rate_series`` when set; otherwise adopt Kavita's
   ``series_rating`` into ``book.rating`` when Kavita has a non-``None`` rating. No other
   series metadata (genres, status, publisher, links) is read or written.
3. Join Kavita's reported anchors onto the book's own chapter table
   (:func:`~ebookerr_sdk.providers.anchoring.join_anchors`, ``CHC-D12``; Kavita has no
   href of its own, so this join is always title-mode). When the join is inconsistent —
   including a re-index wait that timed out — Kavita's chapter table does not describe the
   file on disk, and every read-position operation below (restore, re-anchor, capture) is
   skipped for this sync; metadata, rating and ids are still synced, using whatever page
   Kavita currently reports.
4. Restore the read position via one of two mutually-exclusive branches, both delegated to
   the core anchoring orchestrator: a pending cross-pull restore marker
   (``restore_target``) runs :func:`~ebookerr_sdk.providers.anchoring.restore_to_target`,
   which writes over Kavita's own bookmark unconditionally because the user asked for this
   position; otherwise :func:`~ebookerr_sdk.providers.anchoring.reanchor_bookmark` checks
   whether Kavita's own bookmark still resolves and re-anchors it when it does not
   (``RP-D9``). This is Kavita's **only** restore layer: its scan model never resets the page
   position within one sync the way a Komga re-analyse does, so there is no raw-locator
   snapshot/fast-path to maintain here — Kavita has no raw-locator layer at all (its API
   exposes only a raw page number, never a resumable locator payload), so ``RP-PLUG-4``'s
   fast path does not apply and ``sync`` never sets ``external_locator``.
5. Write the ``external_*`` fields (provider, ids, progress, deep link, timestamps) and
   capture the semantic read position through the core anchoring orchestrator
   (:func:`~ebookerr_sdk.providers.anchoring.capture_position`, ``KAVITA-FACT-2``)
   for the caller to change-gate and store (``RP-CAP-5``); the captured chapter's title and
   key come from the joined chapter table, not from Kavita's own (often title-less) TOC
   (``RP-D20``).

The service makes no DB writes itself: every outcome is returned as ``SyncResult.fields``
plus an optional ``SyncResult.read_position``, for the caller
(:class:`~src.plugins.kavita_sync.KavitaSyncPlugin`) to persist through the standard
apply path. Kavita being disabled/unreachable is non-fatal: :meth:`KavitaService.enrich`
returns a not-``ok`` result on any failure, ``attempted=True`` except when an open
circuit breaker (``EXP-269``) answers unreachable from memory instead of probing;
:meth:`KavitaService.sync` returns ``attempted=False`` when disabled or when that same
open breaker skips the call, and ``attempted=True`` for every other outcome (unreachable,
not found, or success) — the caller logs ``message`` and carries on either way.
Deleting a book is **nudge-only**:
Kavita has no direct delete API. When a deleted book has a stored ``external_library_id``,
:meth:`KavitaService.rescan_library_after_delete` asks Kavita to rescan that library by
id, and Kavita's own scan drops the missing file from its library. When the book has no
library id but ``library_path`` is configured, :meth:`KavitaService.nudge_folder_after_delete`
asks Kavita to rescan the book's former parent folder (``EXP-193``, ``EXP-199``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ebookerr_sdk.domain.dates import log_clock, parse_datetime
from ebookerr_sdk.providers.anchoring import (
    AnchorJoin,
    capture_position,
    join_anchors,
    reanchor_bookmark,
    restore_to_target,
)
from ebookerr_sdk.providers.connection import ProviderUnreachable
from ebookerr_sdk.providers.link_attempt import file_changed_since_attempt
from ebookerr_sdk.providers.link_refusal import LinkOwnerLookup, LinkRefusal
from ebookerr_sdk.providers.scan_ledger import ScanLedger, file_changed_at
from ebookerr_sdk.providers.urls import deep_link_base
from ebookerr_sdk.readpos import (
    backward_move_message,
    db_backup_capture,
    is_empty_position,
    moves_backwards,
)
from ebookerr_sdk.spi import (
    BookView,
    CircuitGuard,
    CircuitOpenError,
    ProviderAnchor,
    ProviderBookmark,
    ReadPosition,
)

from src.gateways.kavita_client import KavitaRef, KavitaSeriesUnresolved
from src.gateways.protocols import KavitaClient

logger = logging.getLogger(__name__)

_CONN_OK_TTL_S = 60.0


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of one :meth:`KavitaService.sync` or :meth:`KavitaService.enrich` call.

    Carries no side effects itself — the caller
    (:class:`~src.plugins.kavita_sync.KavitaSyncPlugin`) wraps ``fields``/``read_position``
    into a ``BookPatch`` for the single apply path to persist.

    Attributes:
        ok: Whether the call reached and updated/read Kavita successfully.
        message: Short human-readable outcome, logged by the caller.
        attempted: ``False`` only when :meth:`KavitaService.sync` is called while Kavita
            sync is disabled; every other outcome, and every
            :meth:`KavitaService.enrich` call regardless of outcome, reports ``True``.
        fields: Column -> value mapping of outcome fields for the caller to persist
            (ids, timestamps, reading state, rating).
        read_position: Semantic read position captured from the TOC + current page
            (``RP-CAP-5``), or ``None`` when the book has no usable TOC or is unread.
        backward_move: A user-safe message when the read position moved backwards
            (``EXP-123``), built by
            :func:`~ebookerr_sdk.readpos.backward_move_message` — both chapter
            indices and both in-chapter percentages (``EXP-207``); ``None`` when no
            backward move occurred.
        restore_attempted: ``True`` when the sync reached the book and attempted the restore —
            an accepted write, a rejected write, or no chapter match alike — so the
            one-shot marker is consumed and the history entry stays for a manual retry;
            ``False`` when the sync never reached the book (``EXP-155``).
        restore_landed: ``True`` only when the provider accepted the restore write this call
            carried. A caller must never infer "landed" from the app's own stored position
            (``EXP-191``).
        unreachable: ``True`` when the provider could not be reached — the caller logs the
            per-book line at DEBUG, the service having logged the outage once (``EXP-192``).
        relinked: ``True`` when this sync or enrich resolved the book to a Kavita chapter id
            different from the one it was linked to. A first link (no stored id) is never a relink.
        stale_link_kept: The per-book record when this book's own file is at a provider chapter
            another library row owns, so the stale link was kept (``F5c``); ``None`` otherwise.
            Unlike Komga, the sync reports ``ok=False`` when this fires — Kavita cannot carry on
            against a chapter it did not resolve.
        refused: ``True`` when a local ownership check refused the link
            (see :class:`~ebookerr_sdk.providers.link_refusal.LinkRefusal`) — a terminal,
            as-designed outcome the caller reports as a skip, not a failure (``EXP-243``,
            ``SPI 2.19``).
        not_found: ``True`` when Kavita has no book matching this one yet — an as-designed
            outcome the caller reports as a skip, not a failure (``DFT-TR-5``): the provider's
            own indexing has not caught up, not a real error, and the deferred lane retries it
            on its own schedule regardless.
    """

    ok: bool
    message: str = ""
    attempted: bool = True
    fields: Mapping[str, Any] = field(default_factory=dict)
    read_position: ReadPosition | None = None
    backward_move: str | None = None
    restore_attempted: bool = False
    restore_landed: bool = False
    unreachable: bool = False
    relinked: bool = False
    stale_link_kept: str | None = None
    refused: bool = False
    not_found: bool = False


def _utc_now() -> datetime:
    """Return the current UTC time (injectable as ``KavitaService``'s ``now``)."""
    return datetime.now(tz=UTC)


def _span_from(anchors: Sequence[ProviderAnchor], start: int, total_pages: int) -> int:
    """Page span of the anchor starting at *start*: to the next anchor, else to the book's end."""
    later = [int(a.ref) for a in anchors if int(a.ref) > start]
    return max(1, (min(later) - start) if later else (total_pages - start))


class KavitaService:
    """Synchronise one downloaded book with Kavita (see module docstring)."""

    def __init__(
        self,
        client: KavitaClient,
        *,
        enabled: bool,
        server_url: str = "",
        external_url: str | None = None,
        library_folder: Path | None = None,
        library_path: str | None = None,
        scan_retry_max: int = 20,
        scan_retry_delay: float = 2,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        link_owner: LinkOwnerLookup | None = None,
        circuit: CircuitGuard | None = None,
        scan_requests: ScanLedger | None = None,
    ) -> None:
        """Wire the Kavita client and sync configuration.

        Args:
            client: The Kavita REST client (``KavitaClient`` protocol).
            enabled: Whether Kavita sync is turned on; ``sync``/``enrich`` short-circuit
                with a non-``ok`` result when ``False`` (see :class:`SyncResult`'s
                ``attempted`` for how the two methods report this differently).
            server_url: The Kavita server URL (base; used for deep links when
                ``external_url`` is unset).
            external_url: Optional browser-facing Kavita URL used to build deep links;
                when unset, falls back to ``server_url`` (``EXP-197``).
            library_folder: The library root EPUBs live under (``SPI 2.15``).
            library_path: The folder Kavita sees the library at, e.g. ``/books``;
                ``None`` disables folder-scan requests, which Kavita cannot act on
                without it (``EXP-193``, ``EXP-199``).
            scan_retry_max: Maximum poll attempts :meth:`_wait_for_reindex` makes for a
                changed file to come back re-indexed under a fresh chapter id.
            scan_retry_delay: Seconds between those polls; a non-positive value floors
                to 2.0.
            sleep: Injectable sleep function (tests pass a no-op).
            now: Injectable UTC-now function (tests pass a fixed clock).
            monotonic: Injectable monotonic-clock function backing the connection-check
                TTL cache (tests pass a fake clock).
            link_owner: Answers "which book already holds this Kavita chapter id?" (``SPI 2.13``
                ``ctx.provider_link_owner``). ``None`` disables the exclusivity check. The
                service passes its own provider name, so a row linked to the other provider
                can never refuse this one.
            circuit: The app's shared circuit breakers (``SPI 2.21``). ``None`` keeps the
                pre-2.18.29 behaviour — a probe per call — and is only for a test that is not
                about reachability.
            scan_requests: The run's scan-request ledger, kept in the plugin's state
                (``DFT-D21``, ``PMG-D32``); ``None`` scans every time; when set, skips a scan if
                an earlier request already covers this file's change time.
        """
        self._client = client
        self._enabled = enabled
        self._server_url = server_url
        self._external_url = external_url
        self._library_folder = library_folder
        self._library_path = library_path.rstrip("/") if library_path else None
        self._scan_retry_max = scan_retry_max
        self._scan_retry_delay = scan_retry_delay if scan_retry_delay > 0 else 2.0
        self._sleep = sleep
        self._now = now
        self._monotonic = monotonic
        self._link_owner = link_owner
        self._circuit = circuit
        self._circuit_key = "provider:kavita"
        self._circuit_label = "Kavita"
        self._conn_ok_until: float = 0.0
        self._outage_logged = False
        self._scan_requests = scan_requests
        # Per-call memo of the KavitaRef this call resolved, keyed by str(chapter_id): the SPI 2.19
        # anchoring primitives need volume/series/library ids and the page total, and none of that
        # is in the TOC. Cleared at the top of _sync_reachable/_enrich_reachable.
        self._ref_cache: dict[str, KavitaRef] = {}

    def _connection_ok(self) -> bool:
        """test_connection() with a 60s success cache (per service instance).

        A successful probe is cached for 60 s; any transport failure inside a call
        drops the cache (``_unreachable_result``), so a provider that dies mid-batch
        is re-probed by the next book (``EXP-192``).
        """
        now = self._monotonic()
        if now < self._conn_ok_until:
            return True
        ok = self._client.test_connection().ok
        if ok:
            self._conn_ok_until = now + _CONN_OK_TTL_S
        return ok

    def _reachable_or_result(self, book: BookView) -> SyncResult | None:
        """Return an unreachable result when the provider is known to be down, else ``None``.

        A breaker that is already open answers from memory: no probe, no timeout, no
        network. That is the whole of ``EXP-269`` — the message this service already prints
        ("every later book in this batch is reported unreachable") becomes true instead of
        costing a full 30 s ``ReadTimeout`` per book.

        Args:
            book: The book about to be synced or enriched.

        Returns:
            A ``SyncResult`` to return immediately, or ``None`` when the call may proceed.
        """
        if self._circuit is not None and self._circuit.is_open(self._circuit_key):
            logger.debug(
                'Kavita circuit is open — "%s" reported unreachable without a probe', book.title
            )
            return self._unreachable_result(book, "circuit open", attempted=False)
        try:
            with self._guard():
                if not self._connection_ok():
                    raise ProviderUnreachable("connection probe failed")
        except CircuitOpenError:
            return self._unreachable_result(book, "circuit open", attempted=False)
        except ProviderUnreachable as exc:
            return self._unreachable_result(book, str(exc))
        return None

    def _guard(self) -> AbstractContextManager[None]:
        """The breaker around one provider call, or a no-op when none was injected."""
        if self._circuit is None:
            return nullcontext()
        return self._circuit.guard(self._circuit_key, label=self._circuit_label)

    def _nudge_due(self, book: BookView) -> str | None:
        """Why a folder scan is due: ``"first attempt"``, ``"file changed"``, or ``None``.

        An unlinked book is nudged once on its first attempt (``EXP-199``); any book — linked
        or not — is nudged again when its file changed since the last recorded attempt
        (``EXP-202``).
        """
        if not book.output_filename:
            return None
        if not book.external.item_id and book.external.link_attempted_at is None:
            return "first attempt"
        if file_changed_since_attempt(
            link_attempted_at=book.external.link_attempted_at,
            link_attempt_size=book.external.link_attempt_size,
            file_size=book.file_size,
        ):
            return "file changed"
        return None

    def _unreachable_result(
        self, book: BookView, detail: str, *, attempted: bool = True
    ) -> SyncResult:
        """Drop cache, log outage once per instance, and report unreachable (``EXP-192``).

        Args:
            book: The book for which the provider is unreachable (used for logging).
            detail: The error detail to log (e.g., "connection probe failed").
            attempted: Whether to report ``attempted=True`` in the result (default True).

        Returns:
            A :class:`SyncResult` with ``ok=False``, ``unreachable=True``, and
            ``attempted`` set as specified.
        """
        self._conn_ok_until = 0.0
        if not self._outage_logged:
            self._outage_logged = True
            logger.warning(
                'Kavita is not reachable (%s) — sync skipped for "%s"; every later book in this '
                "batch is reported unreachable, not missing",
                detail,
                book.title,
            )
        else:
            logger.debug('Kavita still not reachable — sync skipped for "%s"', book.title)
        return SyncResult(
            False,
            "Kavita is not reachable",
            attempted=attempted,
            unreachable=True,
            fields={"external_chapter_count": None},
        )

    def _refusal_for(self, chapter_id: str, book: BookView) -> LinkRefusal | None:
        """Whether *book* may claim Kavita chapter *chapter_id*, or a refusal reason.

        Kavita resolves a book by its library path, so two library rows sharing an
        ``output_filename`` resolve to one chapter; from there they share a reading position
        and overwrite each other's metadata, the same defect measured on Komga (``EXP-149``).
        A refusal is a **local, terminal** answer: the caller stops immediately, never scans
        or polls the provider for it, and reports the local cause — the owning row — instead
        of the provider's "not found" (``EXP-187``).

        Args:
            chapter_id: The chapter id ``find_chapter`` just resolved.
            book: The book trying to claim it.

        Returns:
            ``None`` when the id is unclaimed or already this book's; a
            :class:`LinkRefusal` when a different book owns it, having logged one WARNING
            naming both.
        """
        if self._link_owner is None:
            return None
        owner = self._link_owner(chapter_id, provider="kavita")
        if owner is None or owner == book.book_id:
            return None
        logger.warning(
            'Refused to link "%s" (book_id=%s) to Kavita chapter %s: '
            "book_id=%s already owns it — leaving this book unlinked",
            book.title,
            book.book_id,
            chapter_id,
            owner,
        )
        return LinkRefusal(
            provider="Kavita", noun="chapter", item_id=chapter_id, owner_book_id=owner
        )

    def _epub_path_for(self, book: BookView | None) -> Path | None:
        """The book's EPUB in the library, or ``None`` when either half is unknown.

        Args:
            book: The merged book view, or ``None``.

        Returns:
            The path, or ``None`` when there is no library folder or no output filename.
        """
        if book is None or not self._library_folder or not book.output_filename:
            return None
        return self._library_folder / book.output_filename

    def _build_join(self, book: BookView, chapter_id: str | int) -> AnchorJoin:
        """Join Kavita's anchors onto the book's own chapter table (``CHC-D12``).

        The one place `join_anchors` is called — `_enrich_reachable`'s read-only capture,
        `_sync_reachable`'s read-position work and `_wait_for_reindex`'s poll loop all join the
        same way, so they can never disagree on which of Kavita's anchors is which of the
        book's own chapters. Kavita addresses chapters by neither href nor a stable id, so this
        always joins in title mode.

        Args:
            book: The book whose chapter table the anchors are joined onto.
            chapter_id: The Kavita chapter id currently linked, whose anchors are listed.

        Returns:
            The join, with its own consistency verdict.
        """
        return join_anchors(
            self.list_anchors(str(chapter_id)),
            book.chapter_table,
            None,
            book_title=book.title,
        )

    def enrich(self, book: BookView) -> SyncResult:
        """Read-only Kavita lookup: find chapter, read back progress + rating (F11).

        No ``scan_folder``, no rating push, no progression write — the only side effect
        is adopting Kavita's rating into the returned fields when the local rating is
        unset. Operates on the merged ``BookView`` snapshot (EDIT-D14). A chapter another
        library row already owns is refused with a message naming that row, after one
        WARNING naming both books (``EXP-149``, ``EXP-187``).

        Args:
            book: The merged book view snapshot to look up, matched in Kavita by
                its ``output_filename``.

        Returns:
            A :class:`SyncResult`. ``attempted`` is ``True`` except when an open circuit
            breaker (``EXP-269``) answers unreachable from memory without probing. ``ok``
            is ``False`` (never raises) when Kavita is disabled/unreachable,
            ``book.output_filename`` is unset, or no matching chapter is found in
            Kavita. Never attempts a restore, so its result always reports
            ``restore_attempted=False``.
        """
        if not self._enabled:
            return SyncResult(False, "Kavita sync is disabled", attempted=True)
        blocked = self._reachable_or_result(book)
        if blocked is not None:
            return blocked
        if not book.output_filename:
            return SyncResult(False, "no output filename", attempted=True)
        try:
            with self._guard():
                return self._enrich_reachable(book)
        except CircuitOpenError:
            return self._unreachable_result(book, "circuit open", attempted=False)
        except ProviderUnreachable as exc:
            return self._unreachable_result(book, str(exc))

    def _enrich_reachable(self, book: BookView) -> SyncResult:
        """Read-only lookup when provider is reachable (``EXP-192``).

        A resolved chapter id that differs from the stored one is reported as
        ``SyncResult.relinked`` and logged once at INFO.

        Args:
            book: The merged book view snapshot to look up.

        Returns:
            A :class:`SyncResult`.
        """
        self._ref_cache.clear()
        ref = self._client.find_chapter(book.output_filename or "")
        logger.debug('Kavita enrich for "%s": linked=%s', book.title, ref is not None)
        if isinstance(ref, KavitaSeriesUnresolved):
            logger.warning(
                'Kavita lookup for "%s" matched the file but not its series: %s',
                book.title,
                ref.detail,
            )
            return SyncResult(False, ref.message, attempted=True)
        if ref is None:
            return SyncResult(False, "book not found in Kavita", attempted=True, not_found=True)
        self._ref_cache[str(ref.chapter_id)] = ref
        refusal = self._refusal_for(str(ref.chapter_id), book)
        if refusal is not None:
            return SyncResult(False, refusal.message, attempted=True, refused=True)

        # Kavita re-indexes a file under a new chapter id whenever the folder scan runs, so an
        # id change is normal — but it must be visible, exactly as it is on the other provider.
        relinked = bool(book.external.item_id) and str(ref.chapter_id) != book.external.item_id
        if relinked:
            logger.info(
                'Kavita chapter changed for "%s" (book_id=%s): %s -> %s',
                book.title,
                book.book_id,
                book.external.item_id,
                ref.chapter_id,
            )

        progress = self._client.get_progress(ref.chapter_id)
        fields = self._write_back(book, ref, progress)

        if book.rating is None:
            kavita_rating = self._client.series_rating(ref.series_id)
            if kavita_rating is not None:
                fields = {**fields, "rating": int(kavita_rating)}

        read_position = capture_position(
            self,
            str(ref.chapter_id),
            book=book,
            join=self._build_join(book, ref.chapter_id),
            stored=book.read_position,
            captured_at=self._now().isoformat(),
            provider_name="Kavita",
            book_title=book.title,
        )

        return SyncResult(
            True, "enriched", fields=fields, read_position=read_position, relinked=relinked
        )

    def sync(self, book: BookView, *, restore_target: ReadPosition | None = None) -> SyncResult:
        """Find/rate/read-back one book in Kavita and write the external_* fields.

        See the module docstring for the full numbered sequence. Makes no DB write
        itself — the caller persists ``SyncResult.fields``/``read_position``.
        Operates on the merged ``BookView`` snapshot (EDIT-D14). On a first link to
        a Kavita chapter that reports page 0, the local percent is backed up to RP
        history before the empty provider state is adopted
        (:meth:`_first_link_backup`, EXP-002); a Kavita chapter that already has a
        page still wins unconditionally, exactly as before. A chapter another library
        row already owns is refused with a message naming that row, after one WARNING
        naming both books (``EXP-149``, ``EXP-187``).

        Args:
            book: The merged book view snapshot to sync.
            restore_target: A pending cross-pull restore marker. When set and the sync is
                not stale (see the module docstring),
                :func:`~ebookerr_sdk.providers.anchoring.restore_to_target` matches it
                against the joined chapter table and ``save_progress`` writes the computed
                page back before the rest of the sync runs; a failed match or a rejected
                write is logged by the core and the sync falls back to Kavita's own
                current page.

        Returns:
            A :class:`SyncResult`. ``ok`` is ``False`` (never raises) when Kavita is
            disabled (``attempted=False``), unreachable, or the book is not found in
            Kavita. ``attempted`` is ``False`` for a disabled provider or when an open
            circuit breaker (``EXP-269``) skips the call from memory; ``True`` for every
            other outcome, including a probe or call that itself failed. The caller logs
            ``message`` and carries on. ``restore_attempted`` is ``True`` only when a
            semantic restore was performed against the resolved Kavita chapter; a caller
            consumes the one-shot restore marker on that flag, never on the marker's
            mere presence (``EXP-155``).
        """
        if not self._enabled:
            return SyncResult(False, "Kavita sync is disabled", attempted=False)
        blocked = self._reachable_or_result(book)
        if blocked is not None:
            return blocked
        try:
            with self._guard():
                return self._sync_reachable(book, restore_target=restore_target)
        except CircuitOpenError:
            return self._unreachable_result(book, "circuit open", attempted=False)
        except ProviderUnreachable as exc:
            return self._unreachable_result(book, str(exc))

    def _sync_reachable(  # noqa: C901
        self, book: BookView, *, restore_target: ReadPosition | None = None
    ) -> SyncResult:
        """Sync when provider is reachable (``EXP-192``).

        A book whose **stored** chapter id another library row owns is refused before the
        folder-scan nudge, so a refusal never costs a provider call (``EXP-187``).
        A resolved chapter id that differs from the stored one is reported as
        ``SyncResult.relinked`` and logged once at INFO.
        A chapter another row owns is refused; when this book was already linked to a different
        chapter, the message says the stale link was kept and the outcome is recorded on the book
        (``F5c``).
        With no restore_target, the core anchoring orchestrator decides whether Kavita's bookmark
        still resolves and re-anchors it when it does not (``RP-D9``). All of that — restore,
        re-anchor and capture alike — is skipped for this sync when Kavita's chapter table does
        not describe the file on disk: a file-changed re-index that timed out
        (:meth:`_wait_for_reindex`), or an anchor join that is inconsistent outright
        (``CHC-D12``).
        Folder-scan nudges are deduplicated via a process-wide ledger (``DFT-D21``): when a file
        changed after a scan was already requested, the nudge is skipped and no rescan is asked.

        Args:
            book: The merged book view snapshot to sync.
            restore_target: A pending cross-pull restore marker.

        Returns:
            A :class:`SyncResult`.
        """
        self._ref_cache.clear()
        # EXP-187: a book whose stored id another row owns is refused before any provider call —
        # a nudge for it would look in the log like a failed attempt.
        if book.external.item_id:
            owned = self._refusal_for(book.external.item_id, book)
            if owned is not None:
                return SyncResult(False, owned.message, refused=True)

        output_filename = book.output_filename or ""
        stale = False
        ref: KavitaRef | KavitaSeriesUnresolved | None = None

        why = self._nudge_due(book)
        if why is not None:
            folder = self._provider_folder(book.output_filename)
            if folder is None:
                logger.debug(
                    'Folder-scan nudge skipped for "%s" (%s): '
                    "Kavita library folder is not configured",
                    book.title,
                    book.book_id,
                )
            else:
                ledger_key = f"{self._server_url}|folder:{folder}"
                changed_at = (
                    file_changed_at(self._library_folder / book.output_filename)
                    if self._library_folder is not None and book.output_filename
                    else None
                )
                covered = (
                    self._scan_requests is not None
                    and changed_at is not None
                    and self._scan_requests.covers(ledger_key, changed_at)
                )
                if covered:
                    logger.debug(
                        'Folder-scan nudge skipped for "%s" (%s): %s was already asked to scan '
                        "after the file changed",
                        book.title,
                        book.book_id,
                        folder,
                    )
                    ok = True
                else:
                    requested_at = (
                        self._scan_requests.now() if self._scan_requests is not None else 0.0
                    )
                    ok = self._client.scan_folder(folder)
                    if ok:
                        if self._scan_requests is not None:
                            self._scan_requests.record(ledger_key, requested_at)
                        logger.info(
                            'Asked Kavita to scan %s for "%s" (%s)', folder, book.title, why
                        )
                if ok:
                    if why == "file changed":
                        ref = self._wait_for_reindex(book, output_filename)
                        stale = ref is None
                else:
                    logger.warning(
                        'Kavita did not accept the folder scan %s for "%s"',
                        folder,
                        book.title,
                    )
        elif not book.external.item_id and book.output_filename:
            logger.debug(
                'Folder-scan nudge skipped for "%s" (%s): '
                "already attempted at %s for the same file",
                book.title,
                book.book_id,
                log_clock(book.external.link_attempted_at),
            )

        if ref is None:
            ref = self._client.find_chapter(output_filename)
        if isinstance(ref, KavitaSeriesUnresolved):
            logger.warning(
                'Kavita lookup for "%s" matched the file but not its series: %s',
                book.title,
                ref.detail,
            )
            return SyncResult(False, ref.message)
        if ref is None:
            return SyncResult(False, "book not found in Kavita", not_found=True)
        self._ref_cache[str(ref.chapter_id)] = ref
        refusal = self._refusal_for(str(ref.chapter_id), book)
        if refusal is not None:
            if book.external.item_id and book.external.item_id != str(ref.chapter_id):
                # F5c: this book IS still linked, just to the wrong chapter. Kavita cannot carry
                # on against a chapter it did not resolve, so the sync ends not-ok — but the
                # record says the stale link was kept, not that the book was never linked.
                logger.warning(
                    'Kavita link for "%s" (book_id=%s) should move to chapter %s (%r), but '
                    "book_id=%s already owns it — keeping the stale link %s",
                    book.title,
                    book.book_id,
                    ref.chapter_id,
                    book.output_filename,
                    refusal.owner_book_id,
                    book.external.item_id,
                )
                return SyncResult(
                    False,
                    refusal.stale_link_message,
                    stale_link_kept=refusal.stale_link_message,
                    refused=True,
                )
            return SyncResult(False, refusal.message, refused=True)

        # Kavita re-indexes a file under a new chapter id whenever the folder scan runs, so an
        # id change is normal — but it must be visible, exactly as it is on the other provider.
        relinked = bool(book.external.item_id) and str(ref.chapter_id) != book.external.item_id
        if relinked:
            logger.info(
                'Kavita chapter changed for "%s" (book_id=%s): %s -> %s',
                book.title,
                book.book_id,
                book.external.item_id,
                ref.chapter_id,
            )

        fields: dict[str, Any] = {}

        if book.rating is not None:
            self._client.rate_series(ref.series_id, float(book.rating))
        else:
            kavita_rating = self._client.series_rating(ref.series_id)
            if kavita_rating is not None:
                fields["rating"] = int(kavita_rating)

        progress = self._client.get_progress(ref.chapter_id)
        page_num = int(progress.get("pageNum", 0))

        # Built once (CHC-D12): the restore/re-anchor branch below, the stale gate and the
        # joined chapter count all read this same join, so they can never disagree on which
        # of Kavita's anchors is which of the book's own chapters.
        join = self._build_join(book, ref.chapter_id)
        # An empty anchor list is "nothing to check this run", not "stale" — reanchor_bookmark
        # and capture_position each already have their own not-applicable/no-anchors branch for
        # it (their own ladders' earlier step), so only a genuinely inconsistent non-empty join
        # skips read-position work here.
        stale = stale or (bool(join.anchors) and not join.consistent)
        if stale:
            logger.warning(
                'Read-position capture, re-anchor and restore skipped for "%s" (book_id=%s): '
                "Kavita's chapter table does not describe the file on disk (%d chapter(s) "
                "have no anchor)",
                book.title,
                book.book_id,
                len(join.unanchored_ordinals),
            )

        restore_attempted = False
        restore_landed = False

        if stale:
            pass  # restore_attempted stays False so the one-shot marker survives (EXP-155).
        elif restore_target is not None:
            # Semantic restore (RP-REST-4): replaces the pre-2.4 chapter_id-gated page restore,
            # which broke across EPUB updates (the id gate and raw pageNum are both unstable).
            restore_attempted = True
            outcome = restore_to_target(
                self,
                str(ref.chapter_id),
                target=restore_target,
                join=join,
                book_title=book.title,
                provider_name="Kavita",
            )
            restore_landed = outcome.written
            if outcome.written:
                page_num = int(self._client.get_progress(ref.chapter_id).get("pageNum", 0))
                logger.info(
                    'Restored semantic read position for "%s": page=%d (chapter_index=%d)',
                    book.title,
                    page_num,
                    restore_target.chapter_index,
                )
        else:
            # RP-D9: outside the marker window the core decides whether Kavita's own
            # bookmark still resolves, and re-anchors it when it does not. Kavita's
            # bookmark can only be *lost* (it names a page, never an href) — the core
            # does not distinguish that from a dead one.
            outcome = reanchor_bookmark(
                self,
                str(ref.chapter_id),
                book=book,
                stored=book.read_position,
                join=join,
                book_title=book.title,
                provider_name="Kavita",
            )
            if outcome.written:
                page_num = int(self._client.get_progress(ref.chapter_id).get("pageNum", 0))
                logger.debug(
                    'Re-read the Kavita page for "%s" after a re-anchor: pageNum=%d',
                    book.title,
                    page_num,
                )

        # Preserve lastModifiedUtc from the original progress, updating pageNum to the
        # (possibly-restored) value.
        write_back_progress = {**progress, "pageNum": page_num}
        fields.update(self._write_back(book, ref, write_back_progress))

        fields["external_chapter_count"] = join.chapter_anchor_count
        logger.info(
            'Kavita reports %d chapter(s) for "%s" (item_id=%s); ebookerr packages %s',
            join.chapter_anchor_count,
            book.title,
            ref.chapter_id,
            book.num_chapters,
        )

        if stale:
            read_position: ReadPosition | None = None
        else:
            read_position = capture_position(
                self,
                str(ref.chapter_id),
                book=book,
                join=join,
                stored=book.read_position,
                captured_at=self._now().isoformat(),
                provider_name="Kavita",
                book_title=book.title,
            )
        backward = self._warn_on_backward_move(
            book, read_position, restore_attempted=restore_attempted
        )
        if read_position is None:
            read_position = self._first_link_backup(book, ref, page_num)

        logger.info('Kavita sync finished for "%s": %s', book.title, ", ".join(sorted(fields)))
        return SyncResult(
            True,
            "synced",
            fields=fields,
            read_position=read_position,
            backward_move=backward,
            restore_attempted=restore_attempted,
            restore_landed=restore_landed,
            relinked=relinked,
        )

    def _wait_for_reindex(self, book: BookView, output_filename: str) -> KavitaRef | None:
        """Poll Kavita for a changed file's freshly re-indexed chapter (``EXP-193``, ``CHC-D12``).

        Called only right after a folder scan was accepted for a file-size change. Kavita
        re-indexes a changed file under a **new** chapter id, so this cannot simply poll the
        old one — every attempt re-resolves the file by name and refreshes ``_ref_cache`` with
        whatever it finds, so a caller inspecting anchors mid-poll always sees the latest guess.

        Args:
            book: The book being synced — its title and chapter table are used to judge
                whether a candidate chapter's anchors already describe the file on disk.
            output_filename: The library-relative path to resolve in Kavita.

        Returns:
            The freshly re-indexed ``KavitaRef`` once its anchors join consistently onto
            ``book.chapter_table``, or ``None`` when ``scan_retry_max`` polls pass without
            one — the caller marks the sync stale and proceeds with whatever ``find_chapter``
            next returns.
        """
        for poll in range(1, self._scan_retry_max + 1):
            self._sleep(self._scan_retry_delay)
            ref = self._client.find_chapter(output_filename)
            if isinstance(ref, KavitaRef):
                self._ref_cache[str(ref.chapter_id)] = ref
                join = self._build_join(book, ref.chapter_id)
                if join.consistent:
                    logger.info(
                        'Kavita re-indexed "%s" (chapter_id=%s) after %d poll(s)',
                        book.title,
                        ref.chapter_id,
                        poll,
                    )
                    return ref
        logger.warning(
            'Kavita has not re-indexed "%s" after %d poll(s); read-position work waits for '
            "the next sync",
            book.title,
            self._scan_retry_max,
        )
        return None

    def _warn_on_backward_move(
        self, book: BookView, read_position: ReadPosition | None, *, restore_attempted: bool = False
    ) -> str | None:
        """Warn when a read position moves backwards, and return a user-safe summary (``RP-D18``).

        An empty provider read-back is never a backward move: the provider is reporting that it
        has no position, which the apply path never records (``RP-CAP-3``).
        When ``restore_attempted`` is True and the move is backwards: INFO log only, no user
        message, so no durable notice is created.

        Args:
            book: The merged book view (for prior stored position and title).
            read_position: The newly-captured semantic read position, or ``None``.
            restore_attempted: Whether this sync performed a restore; ``True`` means the
                backward move was requested by the user.

        Returns:
            A user-safe message when a backward move is detected and not requested, or ``None``.
        """
        prev = book.read_position
        if read_position is not None and is_empty_position(read_position):
            logger.debug(
                'Read position for "%s" (book_id=%s) came back empty; the provider holds no '
                "position, so this is not a backward move",
                book.title,
                book.book_id,
            )
            return None
        if prev is None or read_position is None or not moves_backwards(prev, read_position):
            return None
        if restore_attempted:
            logger.info(
                'Read position for "%s" moved backwards by the requested restore: '
                "chapter_index %d -> %d, %.0f%% -> %.0f%%",
                book.title,
                prev.chapter_index,
                read_position.chapter_index,
                prev.chapter_progress * 100,
                read_position.chapter_progress * 100,
            )
            return None
        logger.warning(
            'Read position for "%s" moved backwards: chapter_index %d -> %d, %.0f%% -> %.0f%%',
            book.title,
            prev.chapter_index,
            read_position.chapter_index,
            prev.chapter_progress * 100,
            read_position.chapter_progress * 100,
        )
        return backward_move_message(book.title, prev, read_position)

    def _first_link_backup(
        self, book: BookView, ref: KavitaRef, page_num: int
    ) -> ReadPosition | None:
        """DB-sourced RP-history backup on link/re-link to a progress-less Kavita (EXP-002).

        Fires only when this sync links the book to a Kavita chapter id it was not
        linked to before AND Kavita reports page 0 for it AND local state carries
        progress. A Kavita chapter that already has a page still wins exactly as
        before.
        """
        if book.external.item_id == str(ref.chapter_id):
            return None
        if page_num > 0:
            return None
        capture = db_backup_capture(book, captured_at=self._now().isoformat())
        if capture is not None:
            logger.info(
                'Backing up local read state before first-link adoption for "%s": '
                "chapter_index=%d of %s (local %.0f%%)",
                book.title,
                capture.chapter_index,
                capture.total_chapters,
                (book.progress.percent or 0.0) * 100,
            )
        return capture

    def _provider_folder(self, output_filename: str | None) -> str | None:
        """The folder Kavita knows a book by: ``<library_path>/<parent>``, or ``None``.

        When ``library_path`` is unset or ``output_filename`` is blank, returns ``None``.
        Parent ``"."`` → ``library_path`` alone; trailing slashes stripped, POSIX join.

        Args:
            output_filename: The library-relative path, or ``None``.

        Returns:
            The full provider-visible folder path, or ``None`` when incomplete.
        """
        if not self._library_path or not output_filename:
            return None
        parent = str(PurePosixPath(output_filename).parent)
        if parent == ".":
            return self._library_path
        return f"{self._library_path}/{parent}"

    def rescan_library_after_delete(self, library_id: str, titles: Sequence[str]) -> bool:
        """Ask Kavita to rescan a library by id after deleting books.

        Converts ``library_id`` from string to int; logs at WARNING when conversion fails.
        On success, logs at INFO with the count and titles of deleted books.

        Args:
            library_id: The Kavita library id as a string (converted to int internally).
            titles: The titles of the deleted books (for logging).

        Returns:
            True when the scan request was accepted, False on conversion error or HTTP failure.
        """
        try:
            lib_id = int(library_id)
        except ValueError:
            logger.warning("Cannot ask Kavita to rescan: library id %r is not a number", library_id)
            return False

        ok = self._client.scan_library(lib_id)
        if ok:
            logger.info(
                "Asked Kavita to rescan library %s after deleting %d book(s): %s",
                lib_id,
                len(titles),
                ", ".join(titles),
            )
        else:
            logger.warning(
                "Kavita did not accept the library rescan after deleting %d book(s) "
                "(library_id=%s)",
                len(titles),
                library_id,
            )
        return ok

    def nudge_folder_after_delete(self, output_filename: str | None, title: str | None) -> bool:
        """Ask Kavita to scan the folder a deleted book came from.

        Uses ``_provider_folder`` to compute the scan path. When it is ``None``,
        logs at WARNING and returns ``False`` (cannot ask Kavita without the path).

        Args:
            output_filename: The deleted book's library-relative path, or ``None``.
            title: The deleted book's title (for logging).

        Returns:
            True when the scan request was accepted, False when the path could not
            be computed or the request failed.
        """
        folder = self._provider_folder(output_filename)
        if folder is None:
            logger.warning(
                'Cannot ask Kavita to rescan for deleted "%s": no library id stored and no '
                "Kavita library folder configured",
                title or "(unknown)",
            )
            return False

        ok = self._client.scan_folder(folder)
        if ok:
            logger.info(
                'Asked Kavita to scan %s after deleting "%s"',
                folder,
                title or "(unknown)",
            )
        else:
            logger.warning(
                'Kavita did not accept the folder scan %s after deleting "%s"',
                folder,
                title or "(unknown)",
            )
        return ok

    def _write_back(
        self, book: BookView, ref: KavitaRef, progress: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the external_* fields from a Kavita ref + progress payload.

        ``external_progress_at`` is stamped when the page position or completion changed,
        or when the page total changed and Kavita reported the previous total
        (``external_provider == "kavita"``). A changed total from another provider or on
        first link is re-baselined silently (``EXP-195``).

        Args:
            book: The merged book view snapshot; used for logging when a completion
                date cannot be parsed.
            ref: The resolved Kavita reference (chapter/series/library ids, total pages).
            progress: The raw ``get_progress`` payload; ``"pageNum"`` and
                ``"lastModifiedUtc"`` are read.

        Returns:
            ``external_provider``, ``_library_id``, ``_item_id``, ``_collection_id``,
            ``_item_url`` (:meth:`_deep_link`), ``_read_position``, ``_read_total``,
            ``_read_percent``, ``_read_completed``, ``_synced_at``, ``external_progress_at``
            (when reading state changed), and conditionally ``read_completed_at``
            (when the book reads as completed and Kavita supplies a parseable ``lastModifiedUtc``).
        """
        page_num = int(progress.get("pageNum", 0))
        total = ref.total_pages
        read_percent = page_num / total if total > 0 else 0.0
        completed = 1 if page_num >= total and total > 0 else 0
        fields: dict[str, Any] = {
            "external_provider": "kavita",
            "external_library_id": str(ref.library_id),
            "external_item_id": str(ref.chapter_id),
            "external_collection_id": str(ref.series_id),
            "external_item_url": self._deep_link(ref),
            "external_read_position": page_num,
            "external_read_total": total,
            "external_read_percent": read_percent,
            "external_read_completed": completed,
            "external_synced_at": self._now(),
        }
        # Kavita knows when progress last moved; for a finished book that is when it was
        # finished. Without it, plugin_runtime falls back to now() and ebookerr records "when
        # it noticed". Older Kavita servers omit the field — then the fallback still applies.
        if completed:
            finished_at = parse_datetime(progress.get("lastModifiedUtc"))
            if finished_at is not None:
                fields["read_completed_at"] = finished_at
            else:
                logger.debug(
                    'Kavita reported "%s" completed with no usable lastModifiedUtc (%r)',
                    book.title,
                    progress.get("lastModifiedUtc"),
                )
        same_provider = book.external.provider == "kavita"
        if (
            page_num != (book.progress.position or 0)
            or completed != int(book.progress.completed)
            or (same_provider and total != (book.progress.total or 0))
        ):
            fields["external_progress_at"] = self._now()
        return fields

    def list_anchors(self, item_id: str) -> list[ProviderAnchor]:
        """Return every place in this book the provider can currently put a reader.

        Implements ``SPI 2.19`` ``ReadPositionAnchoring``. A Kavita anchor's ``ref`` is its
        TOC entry's **start page as a string** — Kavita addresses chapters by page, never by
        href, so ``ProviderAnchor.href`` has no Kavita analogue and the core's href facet
        simply never fires here.

        Args:
            item_id: The provider's own id for the book (keyed by str(chapter_id)).

        Returns:
            One ``ProviderAnchor`` per addressable chapter, in the provider's own reading
            order, front matter included. ``[]`` when the provider cannot answer.
        """
        ref = self._ref_cache.get(item_id)
        if ref is None:
            return []
        items = sorted(
            (
                entry
                for entry in self._client.book_chapters(ref.chapter_id)
                if isinstance(entry, dict) and isinstance(entry.get("page"), int)
            ),
            key=lambda entry: entry["page"],
        )
        return [
            ProviderAnchor(ref=str(entry["page"]), title=entry.get("title"), ordinal=ordinal)
            for ordinal, entry in enumerate(items)
        ]

    def read_bookmark(self, item_id: str) -> ProviderBookmark | None:
        """Return where this provider currently says the reader is.

        Implements ``SPI 2.19`` ``ReadPositionAnchoring``. Returns ``None`` when Kavita
        holds no position for the book (``pageNum <= 0``) or when this call never resolved
        a ``KavitaRef``; otherwise a bookmark whose ``ref`` is the containing TOC entry's
        start page — which means a Kavita bookmark always resolves except when there is
        none. Kavita reaches the core's re-anchor branch only via "the provider holds no
        bookmark", never via "the bookmark points at something gone".

        Args:
            item_id: The provider's own id for the book (keyed by str(chapter_id)).

        Returns:
            The parsed bookmark, or ``None`` when the provider holds none or could not be asked.
        """
        ref = self._ref_cache.get(item_id)
        if ref is None:
            return None
        page_num = int(self._client.get_progress(ref.chapter_id).get("pageNum", 0) or 0)
        if page_num <= 0:
            return None
        anchors = self.list_anchors(item_id)
        containing = max(
            (a for a in anchors if int(a.ref) <= page_num),
            key=lambda a: int(a.ref),
            default=None,
        )
        if containing is None:
            return None
        start = int(containing.ref)
        span = _span_from(anchors, start, ref.total_pages)
        return ProviderBookmark(
            ref=containing.ref,
            title=containing.title,
            progression=min(1.0, max(0.0, (page_num - start) / span)),
            raw={"pageNum": page_num},
        )

    def place_bookmark(
        self, item_id: str, anchor: ProviderAnchor, bookmark: ProviderBookmark
    ) -> bool:
        """Put the reader at *anchor*, carrying *bookmark*'s progress across.

        Implements ``SPI 2.19`` ``ReadPositionAnchoring``. A page which still resolves is
        deliberately **not** corrected: ``KAVITA-FACT-3`` drift (a re-scan recomputing page
        counts) is indistinguishable from a genuine reader move and ``RP-PLUG-5`` still
        forbids overwriting live provider progress. Progress 1.0 lands on the chapter's last
        page, never on the next chapter's first (``RP-D20``).

        Args:
            item_id: The provider's own id for the book (keyed by str(chapter_id)).
            anchor: One of the anchors this provider returned from :meth:`list_anchors`.
            bookmark: The bookmark being re-anchored, for its progression and raw payload.

        Returns:
            ``True`` only when the provider accepted the write. Never raise for an ordinary
            rejection — report ``False`` and let the core log it.
        """
        ref = self._ref_cache.get(item_id)
        if ref is None:
            return False
        anchors = self.list_anchors(item_id)
        if anchor.ref not in {a.ref for a in anchors}:
            return False
        start = int(anchor.ref)
        span = _span_from(anchors, start, ref.total_pages)
        # For non-last chapters, clamp to avoid crossing into the next chapter's first page.
        # For the last chapter, clamp to the total pages. "Last" is asked of the anchor list
        # itself (fresh from the TOC), never inferred from ref.total_pages: Kavita's search
        # index — find_chapter's own source — can still report a book's pre-merge page count
        # after its chapter list has already grown, understating total_pages.
        has_later_anchor = any(int(a.ref) > start for a in anchors)
        max_page = start + span - 1 if has_later_anchor else start + span
        if bool(bookmark.raw.get("completed")):
            # A stale "book finished" position may name a chapter no longer last (a later
            # append, e.g. a merge, added one after it) — still clamp to its own span, never
            # to the book's current last page (RP-D20).
            page = max_page
        else:
            page = max(start, min(max_page, start + int(round(bookmark.progression * span))))
        logger.debug(
            "Kavita page for chapter %s: starts at %d, span %d, progress %.2f -> page=%d",
            item_id,
            start,
            span,
            bookmark.progression,
            page,
        )
        return self._client.save_progress(ref, page)

    def _deep_link(self, ref: KavitaRef) -> str | None:
        """Build the book's Kavita series URL from ``external_url``, else ``server_url``.

        Uses :func:`~ebookerr_sdk.providers.urls.deep_link_base` to answer "where does the
        user open Kavita?" — the optional ``external_url`` setting, else ``server_url``.

        Args:
            ref: The resolved Kavita reference (for ``library_id``/``series_id``).

        Returns:
            ``"<base>/library/<library_id>/series/<series_id>"`` where ``base`` is from
            :func:`~ebookerr_sdk.providers.urls.deep_link_base`, or ``None`` when both
            ``server_url`` and ``external_url`` are unset.
        """
        base = deep_link_base(self._server_url, self._external_url)
        if base:
            return f"{base}/library/{ref.library_id}/series/{ref.series_id}"
        return None
