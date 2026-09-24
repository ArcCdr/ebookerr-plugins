"""A ``BookPlugin`` (``priority=900``, the late band) that wraps KomgaService.

Depends on the SPI alone (``EDIT-FR-14``): no data-layer repository or app-settings
injection. Thin adapter that calls ``KomgaService.sync``/``enrich`` for each book.
The service makes no DB writes itself; this plugin wraps its ``SyncResult.fields``/
``read_position`` into ``BookPatch`` objects for the core's single apply path to
persist. A pending ``restore_target`` is consumed only when the call actually attempted the
provider write (:func:`~ebookerr_sdk.providers.restore_marker.restore_marker_patch`, ``EXP-155``); a
read-only ``enrich`` leaves it pending for the next sync.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ebookerr_sdk.providers.link_attempt import LINK_ERROR_FIELD, link_attempt_fields
from ebookerr_sdk.providers.restore_marker import restore_marker_patch
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    InvocationMode,
    PluginContext,
    PluginEventType,
    PluginManifest,
    PluginType,
    SettingsField,
    SettingsSchema,
    UiTrigger,
)

from komga_sync.client import RequestsKomgaClient
from komga_sync.service import KomgaService, SyncResult

logger = logging.getLogger(__name__)


_SCHEMA = SettingsSchema(
    fields=(
        SettingsField(
            key="server",
            type="string",
            label="Server URL",
            required=True,
            help="e.g. http://komga.local:25600",
        ),
        SettingsField(
            key="api_key",
            type="string",
            label="API key",
            secret=True,
            required=True,
            help="Komga → Account settings → API keys. Paste the key itself, not the whole header.",
        ),
        SettingsField(
            key="external_url",
            type="string",
            label="External URL",
            help="optional; the address you open Komga at in your browser "
            "(defaults to the server URL)",
        ),
        SettingsField(
            key="library_id",
            type="string",
            label="Library id",
            required=True,
            help="the Komga library your books are in (shown in its address in Komga); ebookerr asks Komga to scan it when a book is added or changed",
        ),
        SettingsField(
            key="scan_retry_max",
            type="int",
            label="Scan retry attempts",
            default="20",
            help="how many times to poll Komga after a scan",
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
        "{server|No server URL} · library {library_id|No library id} · API key {api_key|No API key}"
    ),
)

_MANIFEST = PluginManifest(
    id="komga_sync",
    name="Komga Sync",
    description=(
        "Publishes your books to a Komga server and reads your reading progress back. "
        "Only one library server can be enabled at a time."
    ),
    version="1.1.0",
    plugin_type=PluginType.BOOK,
    settings_schema=_SCHEMA,
    headless=True,
    headed=True,
    transport="in_image",
    priority=900,
    accepts_list=True,
    exclusive_group="library_server",
    provider="komga",
    delete_mode="purge",
    default_enabled=False,
    deferred=True,  # SPI 2.24 — held while the server is unreachable, never failed (DFT-D13)
    network=True,
    run_timeout_s=1800,
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
            label="Sync to Komga",
            description="Push this book's metadata and cover to your library server.",
        ),
    ),
    testable=True,
    reader_url_template="{external_url}/book/{book_id}/read-epub",
    icon="collections_bookmark",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/komga_sync",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/komga_sync",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
)


def _build_service(ctx: PluginContext) -> KomgaService:
    """Factory to build a KomgaService from plugin context settings (injectable for tests).

    Reads ``app_external_url`` from the plugin context's ``app_setting()`` method,
    decoupling the plugin from the data layer.

    ``library_root`` (``SPI 2.15``) is what lets ``RP-REST-2``'s EPUB-TOC-title fallback
    run (``EXP-194``). ``ctx.circuit`` (``SPI 2.21``) is forwarded straight through so an
    outage this plugin observes is remembered process-wide (``EXP-269``).
    """
    from ebookerr_sdk.providers.scan_ledger import ScanLedger

    settings = ctx.settings
    server = settings.get("server") or ""
    api_key = settings.get("api_key") or ""
    external_url = settings.get("external_url") or None
    library_id = settings.get("library_id") or ""
    scan_retry_max = int(settings.get("scan_retry_max", 20))
    scan_retry_delay = float(settings.get("scan_retry_delay", 2))

    client = RequestsKomgaClient(server, api_key, library_id)
    app_external_url = ctx.app_setting("app_external_url") if hasattr(ctx, "app_setting") else None
    # SPI 2.13: the core answers who owns a Komga id, so a second claimant is left
    # unlinked instead of oscillating metadata with the first (EXP-149).
    return KomgaService(
        client,
        enabled=bool(server and api_key),
        scan_retry_max=scan_retry_max,
        scan_retry_delay=scan_retry_delay,
        external_url=external_url,
        app_external_url=app_external_url,
        library_id=library_id or None,
        server_url=server,
        library_folder=ctx.library_root,
        link_owner=getattr(ctx, "provider_link_owner", None),
        circuit=getattr(ctx, "circuit", None),
        scan_requests=ScanLedger(ctx),
    )


def _log_outcome(logger: logging.Logger, action: str, view: BookView, result: SyncResult) -> None:
    """Log one book's sync/enrich outcome at the level its result warrants.

    WARNING when Komga was contacted but the attempt did not complete; DEBUG for a
    success or for a deliberate skip (Komga disabled, book not found, etc.). When
    Komga is unreachable (``result.unreachable``), log at DEBUG since the service
    has already logged the outage once (``EXP-192``).

    Args:
        logger: The plugin context's logger (surfaces on the Logs page).
        action: ``"enrich"`` or ``"sync"`` — which ``KomgaService`` method ran.
        view: The book view the outcome belongs to.
        result: The ``SyncResult`` returned by the service call.
    """
    if result.ok:
        logger.debug(
            'Komga %s ok for "%s" (book_id=%s, item_id=%s)',
            action,
            view.title,
            view.book_id,
            result.fields.get("external_item_id"),
        )
    elif result.unreachable:
        logger.debug(
            'Komga %s skipped for "%s" (book_id=%s): %s',
            action,
            view.title,
            view.book_id,
            result.message,
        )
    elif result.attempted:
        logger.warning(
            'Komga %s did not complete for "%s" (book_id=%s): %s',
            action,
            view.title,
            view.book_id,
            result.message,
        )
    else:
        logger.debug(
            'Komga %s skipped for "%s" (book_id=%s): %s',
            action,
            view.title,
            view.book_id,
            result.message,
        )


def _handle_deleted(books: tuple[BookView, ...], ctx: PluginContext) -> list[BookPatch]:
    """Delete each book's remote Komga item for a ``BookDeleted`` event.

    Best-effort per book (``KomgaService.delete_remote_book`` never raises); a book with
    no Komga link is a DEBUG-logged no-op. When Komga is unconfigured, the whole event is
    a DEBUG-logged no-op. When the core kept the EPUB, the remote delete is skipped
    and logged at INFO (SCS-D27).

    Args:
        books: The deleted books to remove from Komga.
        ctx: Plugin invocation context (settings, logger, cancellation, progress).

    Returns:
        Always ``[]`` — a deletion leaves nothing to patch.
    """
    server = ctx.settings.get("server") or ""
    api_key = ctx.settings.get("api_key") or ""
    if not (server and api_key):
        ctx.logger.debug("Komga not configured — BookDeleted ignored")
        return []
    service = _build_service(ctx)
    total = len(books)
    # This plugin's manifest declares delete_mode="purge", and the purge deletes the file
    # from the shared library folder. When the core kept the EPUB, purging would destroy the
    # very file the user chose to keep (SCS-D27), so the remote delete is skipped.
    epub_kept = ctx.ui_context.get("epub_kept") == "1"
    for index, view in enumerate(books):
        ctx.check_cancelled()
        if view.external.item_id:
            if epub_kept:
                ctx.logger.info(
                    'Kept the EPUB for "%s" (book_id=%s): this plugin\'s delete removes the '
                    "library file itself, so its copy is left in place",
                    view.title,
                    view.book_id,
                )
            else:
                ctx.logger.info(
                    'Deleting "%s" from Komga (item_id=%s)', view.title, view.external.item_id
                )
                service.delete_remote_book(view.external.item_id)
        else:
            ctx.logger.debug('No Komga link for deleted "%s" — nothing to delete', view.title)
        ctx.report((index + 1) / total * 100.0 if total else 100.0)
    return []


def _apply_link_attempt(
    ctx: PluginContext, view: BookView, result: SyncResult, fields: dict[str, Any]
) -> None:
    """Record this call's provider link attempt outcome onto ``fields``, in place.

    Logs a recovery/failure line at the level the outcome warrants, then, when the sync kept a
    stale link it could not move (``F5c``), overwrites the link-error field with that record
    instead of the plain success/failure message
    :func:`~ebookerr_sdk.providers.link_attempt.link_attempt_fields` would otherwise have written.

    Args:
        ctx: Plugin invocation context (for its logger).
        view: The book the attempt was made for.
        result: The ``SyncResult`` returned by the service call.
        fields: The in-progress patch field mapping, updated in place.
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


def _sync_book(
    ctx: PluginContext, service: KomgaService, view: BookView, allow_scan: bool
) -> BookPatch | None:
    """Sync (or enrich) one book with Komga and build its patch, if it has one.

    ``BookImported`` takes the read-only ``KomgaService.enrich`` path; every other event
    calls ``KomgaService.sync``. A failed attempt is reported through
    ``ctx.report_failure`` (SPI 2.12) and a detected backward read-position move through
    ``ctx.notify``. A refused link is reported to the core as a skip, never a failure (``EXP-243``).
    A pending ``view.restore_target`` passes through to ``sync``, and the
    one-shot restore marker is consumed in the same patch only when ``result.restore_attempted``
    is ``True`` — the marker survives a read-only ``enrich`` or any other call that could not
    attempt the provider write (:func:`~ebookerr_sdk.providers.restore_marker.restore_marker_patch`,
    ``EXP-155``). An unlanded restore is reported as an item failure (``EXP-191``). A stale
    provider link the app could not move is recorded on the book through ``external_link_error``
    with a ``"stale link kept: "`` prefix, and is never a notice.

    Args:
        ctx: Plugin invocation context (event type, notify, report_failure).
        service: The ``KomgaService`` to call.
        view: The book to sync.
        allow_scan: Whether a library scan may be triggered (headed or named-event only).

    Returns:
        A ``BookPatch`` when the book has fields or a read position to persist;
        ``None`` otherwise.
    """
    if ctx.event_type == PluginEventType.BOOK_IMPORTED:
        result = service.enrich(view)
    else:
        result = service.sync(view, allow_scan=allow_scan, restore_target=view.restore_target)

    action = "enrich" if ctx.event_type == PluginEventType.BOOK_IMPORTED else "sync"
    _log_outcome(ctx.logger, action, view, result)
    if result.attempted and not result.ok:
        if result.refused:
            # A refusal is as-designed: another library row already owns this provider item, so
            # this book is deliberately left unlinked. Never a failure (EXP-243, SPI 2.19).
            ctx.report_skip(view.book_id, result.message)
        elif result.not_found:
            # Komga's own indexing has not caught up yet — as-designed, not a real error
            # (DFT-TR-5); the deferred lane retries it on its own schedule regardless.
            ctx.report_skip(view.book_id, result.message)
        else:
            ctx.report_failure(view.book_id, result.message)
    elif not result.attempted and result.unreachable:
        # Breaker was open: as-designed skip, never silent (DFT-FR-17, SPI 2.24).
        ctx.report_skip(view.book_id, result.message)
    if result.backward_move:
        # Surface a backward read-position move as a durable notice plus toast (EXP-123, EXP-218).
        ctx.notify("warning", result.backward_move, durable=True)
    if view.restore_target is not None and result.restore_attempted and not result.restore_landed:
        ctx.report_failure(view.book_id, "read position could not be restored")
        ctx.logger.warning(
            'Read-position restore did not land for "%s" (book_id=%s): chapter_index=%d',
            view.title,
            view.book_id,
            view.restore_target.chapter_index,
        )

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
    _apply_link_attempt(ctx, view, result, fields)
    if fields or result.read_position is not None:
        return BookPatch(
            book_id=view.book_id,
            fields=fields,
            read_position=result.read_position,
            relinked=result.relinked,
        )
    return None


class KomgaSyncPlugin:
    """Sync book metadata with Komga via the KomgaService (``EDIT-FR-14``)."""

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the Komga connection settings schema (server, key, library, retries)."""
        return _SCHEMA

    def test_connection(self, ctx: PluginContext) -> tuple[bool, str]:
        """Test the configured Komga connection, distinguishing why a failure happened.

        Delegates the probe to :meth:`RequestsKomgaClient.test_connection`, which returns a
        :class:`~ebookerr_sdk.providers.connection.ConnectionTestResult`; a non-ok outcome is logged once
        at WARNING (with the outcome kind and the user-safe message) before being unwrapped into
        this hook's ``(bool, str)`` shape (R-D — the hook signature itself is not widened).

        Args:
            ctx: Plugin invocation context; reads ``server``/``api_key``/``library_id`` from
                ``ctx.settings``.

        Returns:
            ``(True, "Connected")`` on success; otherwise ``(False, message)`` where ``message``
            names what to fix (unreachable server, rejected key, or an unresolvable library id).
        """
        settings = ctx.settings
        server = settings.get("server", "")
        api_key = settings.get("api_key", "")
        if not server or not api_key:
            return (False, "Not configured")

        library_id = settings.get("library_id", "")
        client = RequestsKomgaClient(server, api_key, library_id)
        result = client.test_connection()
        if result.ok:
            return (True, "Connected")
        logger.warning(
            "Komga connection test failed: outcome=%s, %s", result.outcome, result.message
        )
        return (False, result.message)

    def enrich(self, books: tuple[BookView, ...], ctx: PluginContext) -> list[BookPatch]:
        """Sync/delete each book with Komga and return the resulting patches.

        ``BookDeleted`` events take a distinct, always-empty-return branch: for each
        book with a Komga link, calls ``KomgaService.delete_remote_book``
        (best-effort, never raises) and returns ``[]`` — nothing to patch, the local
        row is already gone. When Komga is unconfigured, the whole event is a
        DEBUG-logged no-op.

        For every other event, builds one ``KomgaService`` (settings are read fresh
        each call) and, per book: ``BookImported`` -> read-only
        ``KomgaService.enrich``; otherwise -> ``KomgaService.sync``, with
        ``allow_scan`` true only for a headed (user-triggered) invocation or a named
        event — never for the unscoped scheduler batch sync. A pending
        ``view.restore_target`` passes through to ``sync`` as its ``restore_target``,
        and the one-shot restore marker is consumed in the same patch only when the call
        actually attempted the provider write
        (see :func:`~ebookerr_sdk.providers.restore_marker.restore_marker_patch`, ``EXP-155``) —
        a read-only ``enrich`` leaves it pending for the next sync, and an
        attempted-but-failed restore still consumes it, leaving the history entry for a manual
        retry. A book with no patchable fields and no captured read position yields no
        ``BookPatch`` at all. Every per-book outcome is logged — WARNING when the sync was
        attempted and did not complete, DEBUG when it succeeded or when Komga is disabled. A
        failed attempt is also
        reported through ``ctx.report_failure`` (SPI 2.12) so the task layer can
        surface a partial-batch error. Progress is reported through ``ctx.report``
        after every book, on both the delete branch and the sync branch, as a
        percentage in 0–100.

        Args:
            books: The books to process for this event.
            ctx: Plugin invocation context (settings, event type, mode, logger,
                cancellation).

        Returns:
            One ``BookPatch`` per book that has fields or a read position to persist;
            always ``[]`` for a ``BookDeleted`` event.
        """
        if ctx.event_type == PluginEventType.BOOK_DELETED:
            return _handle_deleted(books, ctx)

        service = _build_service(ctx)
        allow_scan = ctx.mode == InvocationMode.HEADED or ctx.event_type is not None

        patches: list[BookPatch] = []
        total = len(books)
        ctx.logger.debug("Komga sync starting over %d book(s)", total)
        for index, view in enumerate(books):
            ctx.check_cancelled()
            patch = _sync_book(ctx, service, view, allow_scan)
            if patch is not None:
                patches.append(patch)
            ctx.report((index + 1) / total * 100.0 if total else 100.0)

        return patches
