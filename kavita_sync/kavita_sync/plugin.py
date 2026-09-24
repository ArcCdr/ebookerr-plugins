"""A ``BookPlugin`` (``priority=900``, the late band) that wraps KavitaService.

Thin adapter around :class:`~kavita_sync.service.KavitaService` (the sync
mechanics — rating cross-sync, progress/TOC read-back, semantic restore — are documented
there); this plugin's own job is routing one ``enrich()`` call per event to the right
service method, translating the schema-driven settings into a service instance, and
converting ``SyncResult`` into ``BookPatch``. One in-image provider of the exclusive
``library_server`` group (the other is ``KomgaSyncPlugin``); only one may be enabled at a
time.

This plugin depends on the SPI alone (EDIT-FR-14): it receives ``BookView`` snapshots
and passes them straight to the service, adding no data access layer of its own.

**Event routing** — settings are read live on every call so a settings save applies
immediately, and one ``KavitaService`` is built per call:

- ``BookImported`` -> read-only ``service.enrich(view)``.
- ``BookDeleted`` -> when Kavita isn't configured, logs at DEBUG and returns ``[]``
  (never crashes unconfigured); otherwise, for each deleted book with a known
  ``library_id``, ``service.rescan_library_after_delete(library_id, titles)`` rescans that
  Kavita library by id (one rescan per distinct library, ``EXP-193``); a book with only an
  ``item_id`` (no library id on record) falls back to
  ``service.nudge_folder_after_delete(output_filename, title)``, which re-scans the parent
  folder instead. Both are **nudge-only** — unlike ``KomgaSyncPlugin``, there is no direct
  Kavita delete API to call. A book with no Kavita link logs at DEBUG and is skipped.
- Any other event -> ``service.sync(view, restore_target=view.restore_target)``.

**Settings** (schema-driven, ``plugin.kavita_sync.*``): ``server`` (Kavita base URL),
``api_key`` (secret), ``external_url`` (optional, used for deep links, defaults to ``server``),
``library_path`` (optional, the folder Kavita sees the library at, e.g. ``/books``, needed for
folder scans of newly downloaded books), ``scan_retry_max``/``scan_retry_delay`` (how long a
sync waits for Kavita to re-index a changed file before giving up, ``CHC-D12``).

**Semantic read-position capture/restore** (SPI 1.4): every call threads
``SyncResult.read_position`` straight into the returned ``BookPatch(read_position=...)``
unchanged — this plugin performs no comparison or storage itself (``RP-CAP-5``,
``RP-PLUG-2``). When ``view.restore_target`` is set, it is passed straight through to
``service.sync``. A pending ``restore_target`` is consumed only when the call actually
attempted the provider write (:func:`~ebookerr_sdk.providers.restore_marker.restore_marker_patch`,
``EXP-155``); a read-only ``enrich`` leaves it pending for the next sync, and an
attempted-but-failed restore still consumes it (``RP-PULL-4``), leaving the entry in history
for a manual retry — it does not retry itself.

**Seeded disabled**: unlike the other bundled in-image plugins, ``kavita_sync`` is seeded
*disabled* by default (the first-party seed predicate excludes it by id) since only one
``library_server`` provider should be active out of the box.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ebookerr_sdk.providers.connection import ProviderUnreachable
from ebookerr_sdk.providers.link_attempt import LINK_ERROR_FIELD, link_attempt_fields
from ebookerr_sdk.providers.restore_marker import restore_marker_patch
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    PluginContext,
    PluginEventType,
    PluginManifest,
    PluginType,
    SettingsField,
    SettingsSchema,
    UiTrigger,
)

from kavita_sync.client import RequestsKavitaClient
from kavita_sync.service import KavitaService, SyncResult

logger = logging.getLogger(__name__)

_SCHEMA = SettingsSchema(
    fields=(
        SettingsField(
            key="server",
            type="string",
            label="Server URL",
            required=True,
            help="e.g. http://kavita.local:5000",
        ),
        SettingsField(
            key="api_key",
            type="string",
            label="API key",
            secret=True,
            required=True,
            help=(
                "Kavita → Settings → Users → your user → API key. Paste the key itself, not a URL."
            ),
        ),
        SettingsField(
            key="external_url",
            type="string",
            label="External URL",
            help="optional; the address you open Kavita at in your browser "
            "(defaults to the server URL)",
        ),
        SettingsField(
            key="library_path",
            type="string",
            label="Library folder",
            help="optional; the folder Kavita sees your library at, e.g. /books — "
            "needed for the folder scan that makes a newly downloaded book visible",
        ),
        SettingsField(
            key="scan_retry_max",
            type="int",
            label="Scan retry attempts",
            default="20",
            help="how many times to poll Kavita after a folder scan",
        ),
        SettingsField(
            key="scan_retry_delay",
            type="int",
            label="Scan retry delay (s)",
            default="2",
            help="seconds between scan polls",
        ),
    ),
    summary=(
        "{server|No server URL} · folder {library_path|No library folder} · "
        "API key {api_key|No API key}"
    ),
)

_MANIFEST = PluginManifest(
    id="kavita_sync",
    name="Kavita Sync",
    description=(
        "Publishes your books to a Kavita server and reads your reading progress back. "
        "Only one library server can be enabled at a time."
    ),
    version="1.1.0",
    plugin_type=PluginType.BOOK,
    settings_schema=_SCHEMA,
    headless=True,
    headed=True,
    priority=900,
    exclusive_group="library_server",
    provider="kavita",
    delete_mode="rescan",
    default_enabled=False,
    deferred=True,  # SPI 2.24 — held while the server is unreachable, never failed (DFT-D13)
    accepts_list=True,
    events=(
        PluginEventType.BOOK_CREATED,
        PluginEventType.BOOK_UPDATED,
        PluginEventType.BOOK_IMPORTED,
        PluginEventType.BOOK_DELETED,
        PluginEventType.EPUB_MODIFIED,  # DFT-D20: a count-preserving EPUB edit reaches the server
    ),
    # The provider's name lives in the provider plugin's own manifest, never in the core
    # (DEC-81): "Sync" alone was one noun away from the sidecar plugin's "Sync files", and
    # the core cannot tell them apart because it must not know either (2.18.25, EXP-262 P2).
    ui_triggers=(
        UiTrigger(
            scope="book_selection_action",
            icon="sync",
            label="Sync to Kavita",
            description="Push this book's metadata and cover to your library server.",
        ),
    ),
    testable=True,
    reader_url_template="{external_url}/library/{library_id}/series/{series_id}/book/{book_id}",
    network=True,
    run_timeout_s=1800,
    icon="library_books",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/kavita_sync",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/kavita_sync",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
)


def _build_service(
    ctx: PluginContext,
    *,
    enabled: bool,
) -> KavitaService:
    """Factory to build a KavitaService from plugin context settings (injectable for tests).

    ``scan_retry_max``/``scan_retry_delay`` govern how long a sync waits for Kavita to
    re-index a changed file before the sync's read-position work is marked stale
    (``CHC-D12``).
    """
    from ebookerr_sdk.providers.scan_ledger import ScanLedger

    settings = ctx.settings
    server = settings.get("server", "")
    api_key = settings.get("api_key", "")
    external_url = settings.get("external_url")
    library_path = settings.get("library_path") or None
    scan_retry_max = int(settings.get("scan_retry_max", 20))
    scan_retry_delay = float(settings.get("scan_retry_delay", 2))

    client = RequestsKavitaClient(server, api_key, external_url=external_url)
    return KavitaService(
        client,
        enabled=enabled,
        server_url=server,
        external_url=external_url,
        library_folder=ctx.library_root,
        library_path=library_path,
        scan_retry_max=scan_retry_max,
        scan_retry_delay=scan_retry_delay,
        link_owner=getattr(ctx, "provider_link_owner", None),
        circuit=getattr(ctx, "circuit", None),
        scan_requests=ScanLedger(ctx),
    )


def _log_outcome(logger: logging.Logger, action: str, view: BookView, result: SyncResult) -> None:
    """Log one book's sync/enrich outcome at the level its result warrants.

    WARNING when Kavita was contacted but the attempt did not complete; DEBUG for a
    success or for a deliberate skip (Kavita disabled, book not found, etc.).

    Args:
        logger: The plugin context's logger (surfaces on the Logs page).
        action: ``"enrich"`` or ``"sync"`` — which ``KavitaService`` method ran.
        view: The book view the outcome belongs to.
        result: The ``SyncResult`` returned by the service call.
    """
    if result.ok:
        logger.debug('Kavita %s ok for "%s" (book_id=%s)', action, view.title, view.book_id)
    elif result.unreachable:
        logger.debug(
            'Kavita %s skipped for "%s" (book_id=%s): %s',
            action,
            view.title,
            view.book_id,
            result.message,
        )
    elif result.attempted:
        logger.warning(
            'Kavita %s did not complete for "%s" (book_id=%s): %s',
            action,
            view.title,
            view.book_id,
            result.message,
        )
    else:
        logger.debug(
            'Kavita %s skipped for "%s" (book_id=%s): %s',
            action,
            view.title,
            view.book_id,
            result.message,
        )


def _notify_backward_move(ctx: PluginContext, result: SyncResult) -> None:
    """Surface a backward read-position move as durable notice (``EXP-123``, ``EXP-218``).

    Records outlive the toast so a scheduled sync with no browser still leaves it in
    the notification centre.

    Args:
        ctx: Plugin context; ``ctx.notify`` publishes the toast and records the notice.
        result: The ``SyncResult`` returned by the service call.
    """
    if result.backward_move:
        ctx.notify("warning", result.backward_move, durable=True)


def _report_sync_problems(ctx: PluginContext, view: BookView, result: SyncResult) -> None:
    """Report a failed attempt or an unlanded restore through the SPI (``EXP-243``).

    A refusal is as-designed — another library row already owns this provider item, so this
    book is deliberately left unlinked — and is reported as a skip, never a failure (SPI 2.19).

    Args:
        ctx: Plugin invocation context (report_skip, report_failure, logger).
        view: The book being synced.
        result: The ``SyncResult`` returned by the service call.
    """
    if result.attempted and not result.ok:
        if result.refused:
            ctx.report_skip(view.book_id, result.message)
        elif result.not_found:
            # Kavita's own indexing has not caught up yet — as-designed, not a real error
            # (DFT-TR-5); the deferred lane retries it on its own schedule regardless.
            ctx.report_skip(view.book_id, result.message)
        else:
            ctx.report_failure(view.book_id, result.message)
    elif not result.attempted and result.unreachable:
        # Breaker was open: as-designed skip, never silent (DFT-FR-17, SPI 2.24).
        ctx.report_skip(view.book_id, result.message)
    if view.restore_target is not None and result.restore_attempted and not result.restore_landed:
        ctx.report_failure(view.book_id, "read position could not be restored")
        ctx.logger.warning(
            'Read-position restore did not land for "%s" (book_id=%s): chapter_index=%d',
            view.title,
            view.book_id,
            view.restore_target.chapter_index,
        )


def _record_link_attempt(
    ctx: PluginContext, view: BookView, result: SyncResult, fields: dict[str, Any]
) -> None:
    """Record this sync's provider link attempt onto *fields*, in place (``EXP-200``).

    A stale link the app could not move is recorded as a per-book ``external_link_error``
    with a ``"stale link kept: "`` prefix, never a notice (F5c).

    Args:
        ctx: Plugin invocation context (logger).
        view: The book being synced.
        result: The ``SyncResult`` returned by the service call.
        fields: The patch's field mapping so far; updated in place with the link-attempt
            fields and, on a kept stale link, ``LINK_ERROR_FIELD``.
    """
    link_attempt_patch = link_attempt_fields(
        reached=result.attempted and not result.unreachable,
        ok=result.ok,
        message=result.message,
        file_size=view.file_size,
        now=datetime.now(UTC),
    )
    if not link_attempt_patch:
        return
    if result.ok and not result.stale_link_kept:
        # Clear any previous error
        if view.external.link_error:
            ctx.logger.info(
                'Provider link recovered for "%s" (book_id=%s); previous failure: %s',
                view.title,
                view.book_id,
                view.external.link_error,
            )
    else:
        # Record the failure
        ctx.logger.debug(
            'Recorded link failure for "%s" (book_id=%s): %s',
            view.title,
            view.book_id,
            result.message,
        )
    fields.update(link_attempt_patch)
    if result.stale_link_kept:
        # F5c: a stale link the app could not move is a per-book record, not a notice.
        fields[LINK_ERROR_FIELD] = result.stale_link_kept


def _handle_deleted(
    books: tuple[BookView, ...], ctx: PluginContext, service: KavitaService, *, enabled: bool
) -> list[BookPatch]:
    """Nudge Kavita to rescan for each deleted book's library or folder (``BookDeleted``).

    Books with a known ``library_id`` are grouped and each distinct library is rescanned
    once with its deleted titles (``EXP-193``); a book with only an ``item_id`` falls back
    to a folder nudge; a book with neither is a DEBUG-logged no-op. When Kavita is
    unconfigured, the whole event is a DEBUG-logged no-op. A provider outage during the
    rescan is caught and reported once, never per book.

    Args:
        books: The deleted books to rescan/nudge Kavita for.
        ctx: Plugin invocation context (settings, logger, progress).
        service: The ``KavitaService`` to call.
        enabled: Whether Kavita is configured (server + api_key present).

    Returns:
        Always ``[]`` — a deletion leaves nothing to patch.
    """
    if not enabled:
        ctx.logger.debug("Kavita not configured — BookDeleted ignored")
        return []

    # Group linked books by library_id
    from collections import defaultdict

    by_library_id: dict[str, list[str]] = defaultdict(list)
    unlinked_views = []

    for view in books:
        if view.external.library_id:
            by_library_id[view.external.library_id].append(view.title or "(unknown)")
        elif view.external.item_id:
            # Has an item_id but no library_id — use folder nudge
            unlinked_views.append(view)
        else:
            # Not linked at all
            ctx.logger.debug(
                'No Kavita link for deleted "%s" — nothing to nudge',
                view.title or "(unknown)",
            )

    total = len(books)
    index = 0

    try:
        # Rescan each distinct library once with its deleted books
        for library_id, titles in by_library_id.items():
            service.rescan_library_after_delete(library_id, titles)
            index = len([b for b in books if b.external.library_id == library_id])
            ctx.report((index) / total * 100.0 if total else 100.0)

        # Nudge folders for unlinked books
        for view in unlinked_views:
            service.nudge_folder_after_delete(view.output_filename, view.title)
            index += 1
            ctx.report((index) / total * 100.0 if total else 100.0)
    except ProviderUnreachable:
        ctx.logger.warning(
            "Kavita is not reachable — rescan after deleting %d book(s) skipped",
            total,
        )
        ctx.report(100.0)
        return []

    return []


def _sync_book(ctx: PluginContext, service: KavitaService, view: BookView) -> BookPatch | None:
    """Sync (or enrich) one book with Kavita and build its patch, if it has one.

    ``BookImported`` takes the read-only ``KavitaService.enrich`` path; every other event
    calls ``KavitaService.sync``. A failed attempt is reported through
    ``ctx.report_failure`` (SPI 2.12) and a detected backward read-position move through
    ``ctx.notify``. A refused link is reported to the core as a skip, never a failure (``EXP-243``).
    A pending ``view.restore_target`` passes through to ``sync``, and the one-shot restore marker is
    consumed in the same patch only when ``result.restore_attempted`` is ``True`` — the marker
    survives a read-only ``enrich`` or any other call that could not attempt the provider write (see
    module :func:`~ebookerr_sdk.providers.restore_marker.restore_marker_patch`, ``EXP-155``). An
    unlanded restore is reported as an item failure (``EXP-191``). Every provider link attempt is
    recorded on the patch through module
    :func:`~ebookerr_sdk.providers.link_attempt.link_attempt_fields` (``EXP-200``). A stale provider
    link the app could not move is recorded on the book through ``external_link_error`` with a
    ``"stale link kept: "`` prefix, and is never a notice.

    Args:
        ctx: Plugin invocation context (event type, notify, report_failure).
        service: The ``KavitaService`` to call.
        view: The book to sync.

    Returns:
        A ``BookPatch`` when the book has fields or a read position to persist;
        ``None`` otherwise.
    """
    if ctx.event_type == PluginEventType.BOOK_IMPORTED:
        result = service.enrich(view)
    else:
        result = service.sync(view, restore_target=view.restore_target)

    action = "enrich" if ctx.event_type == PluginEventType.BOOK_IMPORTED else "sync"
    _log_outcome(ctx.logger, action, view, result)
    _report_sync_problems(ctx, view, result)
    _notify_backward_move(ctx, result)

    fields: dict[str, Any] = dict(result.fields)
    # A one-shot marker is consumed by a sync that actually attempted the provider write, never
    # by the read-only enrich a fresh re-import routes to — that enrich runs one second after the
    # restore, before the provider has scanned the file back in, and cannot deliver it (EXP-155).
    fields.update(
        restore_marker_patch(
            marker_pending=view.restore_target is not None,
            restore_attempted=result.restore_attempted,
        )
    )
    if view.restore_target is not None and not result.restore_attempted:
        ctx.logger.debug(
            'Restore marker left pending for "%s" (book_id=%s): %s did not attempt a '
            "provider write",
            view.title,
            view.book_id,
            action,
        )
    # Record the provider link attempt outcome.
    _record_link_attempt(ctx, view, result, fields)
    if fields or result.read_position is not None:
        return BookPatch(book_id=view.book_id, fields=fields, read_position=result.read_position)
    return None


class KavitaSyncPlugin:
    """Sync book metadata with Kavita via the KavitaService."""

    manifest = _MANIFEST

    def __init__(self) -> None:
        """Initialize the plugin (no dependencies; uses the SPI only)."""
        pass

    def settings_schema(self) -> SettingsSchema:
        """Return this plugin's schema-driven settings fields (server/api_key/etc.)."""
        return _SCHEMA

    def test_connection(self, ctx: PluginContext) -> tuple[bool, str]:
        """Test connection to Kavita server using provided context settings."""
        settings = ctx.settings
        server = settings.get("server", "")
        api_key = settings.get("api_key", "")
        if not server or not api_key:
            return (False, "Not configured")

        external_url = settings.get("external_url")
        client = RequestsKavitaClient(server, api_key, external_url=external_url)
        result = client.test_connection()
        if result.ok:
            return (True, "Connected")
        logger.warning(
            "Kavita connection test failed: outcome=%s, %s", result.outcome, result.message
        )
        return (False, result.message)

    def enrich(self, books: tuple[BookView, ...], ctx: PluginContext) -> list[BookPatch]:
        """Route each book through the KavitaService method matching the firing event.

        ``BookDeleted`` events take a distinct, always-empty-return branch — see
        :func:`_handle_deleted`. For every other event, per book: ``BookImported`` ->
        read-only ``KavitaService.enrich``; otherwise -> ``KavitaService.sync`` — see
        :func:`_sync_book`. Progress is reported through ``ctx.report`` after every book
        on both branches, as a percentage in 0–100.

        Args:
            books: The book views to sync (or, for ``BookDeleted``, to nudge a rescan for).
            ctx: Plugin context; carries the live settings and the firing ``event_type``.

        Returns:
            One ``BookPatch`` per book that produced changed fields or a read position;
            an unconfigured Kavita, an unlinked deleted book, or a no-op sync contribute
            nothing. Always ``[]`` for ``BookDeleted`` (delete is a nudge, never a patch).
        """
        settings = ctx.settings
        server = settings.get("server", "")
        api_key = settings.get("api_key", "")
        enabled = bool(server and api_key)

        service = _build_service(ctx, enabled=enabled)

        if ctx.event_type == PluginEventType.BOOK_DELETED:
            return _handle_deleted(books, ctx, service, enabled=enabled)

        patches: list[BookPatch] = []
        total = len(books)
        ctx.logger.debug("Kavita sync starting over %d book(s)", total)
        for index, view in enumerate(books):
            ctx.check_cancelled()
            patch = _sync_book(ctx, service, view)
            if patch is not None:
                patches.append(patch)
            ctx.report((index + 1) / total * 100.0 if total else 100.0)

        return patches
