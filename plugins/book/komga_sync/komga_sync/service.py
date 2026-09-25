"""Komga sync business rules (see ARCHITECTURE.md §2.2 for the cross-provider contract).

Operates on the merged ``BookView`` snapshot: a user's metadata edits are what gets
pushed to Komga (``EDIT-D14``). Local/FanFicFare metadata is master for everything
except *reading* state, which Komga owns. Auth is a per-request ``X-API-Key`` header;
connection checks probe ``GET /actuator/info`` (:meth:`KomgaService._connection_ok`).
Discovery and library-scan calls are scoped to the configured Komga library id
(``find_book_id`` adds it as an ``allOf`` condition; ``trigger_library_scan`` hits
the library-scoped scan endpoint) so books living in other Komga libraries are never
matched or touched; with no library id configured, the scope condition is simply omitted.

The sync, for one freshly-downloaded book (:meth:`KomgaService.sync`):

1. Snapshot Komga read progression first (only if already linked).
2. Ensure the book exists in Komga: re-``analyze`` if known, else find it by
   unique title+author, else trigger a library scan and poll until it appears;
   ask for a library scan when the file changed since the last link attempt
   (``EXT-D24``, EXP-202), then wait for Komga to re-read it — polling ``get_book``
   for a matching ``sizeBytes`` — before trusting anything it reports about the file's
   chapters (:meth:`KomgaService._wait_for_reread`, ``CHC-D12``).
3. Join Komga's reported anchors onto the book's own chapter table
   (:func:`~ebookerr_sdk.providers.anchoring.join_anchors`, ``CHC-D12``). When the join
   is inconsistent — including a re-read that timed out — Komga's chapter table does not
   describe the file on disk, and every read-position operation below (restore, re-anchor,
   capture) is skipped for this sync; metadata, reading state and ids are still synced.
4. Import any Komga-only tags into ``erotica_tags``, compose a canonical ordered
   tag list (star tag → categories → other tags A→Z; see :func:`_compose_tags`
   for the exact rule), and push local-mastered metadata, PATCHing only changed
   fields (book: title/summary/releaseDate/authors/tags/links; series:
   genres/status/publisher/Story link). Komga stores tags unordered, so tags
   count as changed only when their CI content differs. Every local field is
   re-sanitised (HTML-tag-strip, entity-decode, trim; :func:`sanitize`)
   immediately before this comparison/push, as defence in depth on top of the
   sanitisation already applied where each field was first persisted.
5. Restore the read progression via one of two independently-maintained layers,
   never both in the same sync: the **raw locator fast path**
   (:meth:`KomgaService._restore_progress_if_lost`) runs when the core's anchor check
   (``RP-D9``) reports the provider's bookmark still resolves, and protects this sync's
   own in-flight analyse/scan reset using the exact pre-sync snapshot; the
   **semantic restore**
   (:meth:`KomgaService._restore_semantic`) runs instead, only when a
   cross-pull restore marker is pending, and survives the EPUB having been
   replaced entirely (the raw snapshot cannot, since a replaced EPUB gets new
   Komga-internal position ids on re-analyse). Both layers persist the same
   shape in ``external_locator``: the full progression envelope, never the bare
   locator (EXP-123).
6. Read Komga-mastered reading state back: page count, position, completion,
   rating adoption (see :meth:`KomgaService._read_back` and the decision table
   on :meth:`KomgaService._resolve_rating`).
7. Record the Komga ids + sync timestamps.
8. Capture the semantic read position from the just-refreshed book (``RP-CAP-5``)
   for the caller to change-gate and store.

The service makes no DB writes itself: every outcome above is returned as
``SyncResult.fields`` (plus an optional ``SyncResult.read_position``) for the
caller to persist. Komga being disabled/unreachable is non-fatal: ``sync``
returns an error result and the caller carries on. An open circuit breaker
(``EXP-269``) answers a later call from memory instead of re-probing a server
already known to be down, across ``sync``, ``sync_batch`` and ``enrich`` alike
(see :meth:`KomgaService._reachable_or_result`). Every significant step logs
an INFO/DEBUG record so a full sync is traceable on the Logs page without
reading source code; a non-2xx response from a mutating call additionally logs
the endpoint, payload and response body at DEBUG with the ``X-API-Key`` header
redacted, then logs an error.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.domain.metadata import sanitize, sanitize_multiline
from ebookerr_sdk.domain.text_encoding import nfc
from ebookerr_sdk.providers.anchoring import (
    REANCHOR_FINISHED,
    REANCHOR_NOT_APPLICABLE,
    REANCHOR_RESOLVED,
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

from komga_sync.protocol import KomgaClient

logger = logging.getLogger(__name__)

_CONN_OK_TTL_S = 60.0


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of one :meth:`KomgaService.sync` or :meth:`KomgaService.enrich` call.

    Carries no side effects itself — the caller (a provider plugin) wraps
    ``fields``/``read_position`` into a ``BookPatch`` for the single apply path
    to persist.

    Attributes:
        ok: Whether the call reached and updated Komga successfully.
        message: Short human-readable outcome, logged by the caller.
        attempted: ``False`` only when Komga is disabled/not configured — not
            treated as an error.
        current_page: Komga read position after the sync, or ``None``.
        total_pages: Komga page count after the sync, or ``None``.
        fields: Column -> value mapping of outcome fields for the caller to
            persist (ids, timestamps, reading state, rating).
        read_position: Semantic read position captured post-sync (``RP-CAP-5``),
            or ``None`` when the progression was empty/unreliable.
        metadata_pushed: Whether a metadata PATCH was sent to Komga during
            this sync; used internally for batch run summaries.
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
        relinked: ``True`` only when this sync corrected a **wrong** stored link — the stored id
            named a different Komga book than this book's own file. A Komga id re-issue for the
            same file leaves it ``False``, because the stored reading history is still this
            book's.
        stale_link_kept: The per-book record when this book's own file is at a provider item
            another library row owns, so the stale link was kept (``F5c``); ``None`` otherwise.
            The sync still reports ``ok``.
        refused: ``True`` when a local ownership check refused the link
            (see :class:`~ebookerr_sdk.providers.link_refusal.LinkRefusal`) — a terminal,
            as-designed outcome the caller reports as a skip, not a failure (``EXP-243``,
            ``SPI 2.19``).
        not_found: ``True`` when Komga has no book matching this one yet — an as-designed
            outcome the caller reports as a skip, not a failure (``DFT-TR-5``): the provider's
            own indexing has not caught up, not a real error, and the deferred lane retries it
            on its own schedule regardless.
    """

    ok: bool
    message: str = ""
    attempted: bool = True
    current_page: int | None = None
    total_pages: int | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    read_position: ReadPosition | None = None
    metadata_pushed: bool = False
    backward_move: str | None = None
    restore_attempted: bool = False
    restore_landed: bool = False
    unreachable: bool = False
    relinked: bool = False
    stale_link_kept: str | None = None
    refused: bool = False
    not_found: bool = False


def _utc_now() -> datetime:
    """Return the current UTC time (injectable as ``KomgaService``'s ``now``)."""
    return datetime.now(tz=UTC)


def _ci_set(values: Iterable[Any]) -> set[str]:
    """Build a case-insensitive, Unicode-normalised set of string values (``TXE-D3``)."""
    return {nfc(str(value)).casefold() for value in values}


def _dedupe_ci(values: list[str]) -> list[str]:
    """Drop case-insensitive duplicates, keeping the first occurrence's casing/order.

    Folds Unicode form as well as case before comparing (``TXE-D3``).
    """
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = nfc(value).casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _split_csv(value: str | None) -> list[str]:
    """Sanitise and split a ``", "``-joined field into its non-empty parts."""
    return [t for t in (sanitize(value) or "").split(", ") if t]


_STAR_FULL = "★"  # U+2605 BLACK STAR
_STAR_EMPTY = "☆"  # U+2606 WHITE STAR
_RATING_STARS_RE = re.compile(f"({_STAR_FULL}{{0,5}})({_STAR_EMPTY}{{0,5}})")


def _rating_tag(rating: int) -> str:
    """Canonical star tag for a 1-5 rating, e.g. 3 -> '★★★☆☆'."""
    return _STAR_FULL * rating + _STAR_EMPTY * (5 - rating)


def _rating_from_tag(tag: str) -> int | None:
    """0-5 from one tag — legacy 'rating:n' or star form; None when not a rating tag."""
    if tag.startswith("rating:"):
        try:
            val = int(tag.split(":", 1)[1])
        except ValueError:
            return None
        return val if 0 <= val <= 5 else None
    if not tag:
        return None
    m = _RATING_STARS_RE.fullmatch(tag)
    if m is None:
        return None
    full = len(m.group(1))
    if len(tag) == 5:
        return full
    return full if not m.group(2) and 1 <= full <= 5 else None


def _is_rating_like(tag: str) -> bool:
    """True for any tag the rating machinery manages (legacy prefix or star form)."""
    return tag.startswith("rating:") or _rating_from_tag(tag) is not None


def _compose_tags(
    category: str | None, erotica_tags: str | None, desired_rating: int | None
) -> list[str]:
    """Canonical Komga book tag list — the exact ordering rule (behavioural contract).

    1. Star rating tag, only when ``desired_rating`` ∈ 1–5 (see :func:`_rating_tag`;
       ``desired_rating`` itself comes from the rating decision table on
       :meth:`KomgaService._resolve_rating`).
    2. Categories, in ``category``'s own order (lit's casing — Komga lowercases
       tags server-side).
    3. All other tags — the merged erotica list minus any case-insensitive
       category collision — sorted A→Z (``str.lower``).

    Order-preserving, case-insensitive de-duplication applies throughout, folding Unicode
    form as well as case (``TXE-D3``).

    Example:
        ``category="Het, Slash"``, ``erotica_tags="Noncon, Supernatural"``,
        ``desired_rating=3`` → ``["★★★☆☆", "Het", "Slash", "Noncon", "Supernatural"]``.

    Args:
        category: The book's local ``category`` field (CSV).
        erotica_tags: The book's local ``erotica_tags`` field (CSV).
        desired_rating: The rating to encode as a star tag, or ``None``/outside
            1–5 to omit it.

    Returns:
        The composed tag list in canonical order.
    """
    cats = _dedupe_ci(_split_csv(category))
    cats_ci = _ci_set(cats)
    others = sorted(
        (t for t in _dedupe_ci(_split_csv(erotica_tags)) if nfc(t).casefold() not in cats_ci),
        key=str.lower,
    )
    out: list[str] = []
    if desired_rating is not None and 1 <= desired_rating <= 5:
        out.append(_rating_tag(desired_rating))
    return _dedupe_ci(out + cats + others)


def _komga_only_tags(
    current_tags: list[Any], category: str | None, erotica_tags: str | None
) -> list[str]:
    """Import rule (Komga → lit, every sync): Komga tags not already known locally.

    Before composing the outgoing tag list, filters the current Komga tag list
    for tags that are: not rating-like (:func:`_is_rating_like`), not a category
    (case- and Unicode-form-insensitive, ``TXE-D3``), not already in ``erotica_tags``
    (same fold), and free of the literal ``", "`` CSV separator — such a tag could not
    round-trip the column encoding. The caller appends any surviving tags to
    ``erotica_tags`` for the apply path to persist; ``external_progress_at`` is
    **not** bumped by an import (it tracks reading-state changes only).
    :meth:`KomgaService.enrich` remains strictly read-only and never imports.
    The caller patches ``erotica_tags`` only — ``category`` is never part of the
    import — and, per ``EDIT-D14``, the core drops the patch when ``erotica_tags``
    is user-overridden, so an import lands only once the override is reverted.

    Args:
        current_tags: The book's current tag list as read from Komga.
        category: The book's local ``category`` field (CSV).
        erotica_tags: The book's local ``erotica_tags`` field (CSV).

    Returns:
        Order-preserving, case-insensitively de-duplicated tags to add to
        ``erotica_tags``.
    """
    known = _ci_set(_split_csv(category)) | _ci_set(_split_csv(erotica_tags))
    return _dedupe_ci(
        [
            t
            for t in current_tags
            if isinstance(t, str)
            and ", " not in t
            and not _is_rating_like(t)
            and nfc(t).casefold() not in known
        ]
    )


def _genres(book: BookView) -> list[str]:
    """Sanitised, split ``book.category`` — the value pushed as the Komga series ``genres``."""
    return _split_csv(book.category)


def _upsert_links(
    current: list[dict[str, Any]], desired: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], bool]:
    """Merge desired links into a copy of the current list, keeping links Komga already holds.

    Update a label's URL in place when it differs, append it when missing. Returns the merged
    list and whether anything changed (so the caller can submit ``links`` only when needed).
    """
    result: list[dict[str, Any]] = [dict(link) for link in current]
    changed = False
    for want in desired:
        match = next((link for link in result if link.get("label") == want["label"]), None)
        if match is None:
            result.append({"label": want["label"], "url": want["url"]})
            changed = True
        elif match.get("url") != want["url"]:
            match["url"] = want["url"]
            changed = True
    return result, changed


def _book_patch(
    book: BookView,
    current: dict[str, Any],
    desired_tags: list[str],
    ebookerr_url: str | None = None,
) -> dict[str, Any]:
    """Changed-fields-only ``PATCH /api/v1/books/{id}/metadata`` body.

    Every field is the sanitised local/FanFicFare value (defence in depth on top of
    persistence-time sanitisation), included only when it differs from the current
    Komga value:

    - ``title`` — only when the sanitised local title is non-empty.
    - ``summary`` — the sanitised ``synopsis``, falling back to the sanitised ``description``
      (``""`` when both are unset); unlike ``title`` this can be pushed as an empty string. The
      synopsis is sanitised via :func:`sanitize_multiline` — the multi-line policy,
      idempotent on stored text — so the synopsis is the curated value (user edit,
      sidecar adoption or EPUB backfill), and it is what a reader should see.
    - ``releaseDate`` — ``date_published`` rendered as a date-only ``YYYY-MM-DD``
      string (Komga's own form), only when local is truthy.
    - ``authors`` — ``[{"name": ..., "role": "writer"}]``, only when the sanitised
      local author is non-empty.
    - ``tags`` — compared as **case-insensitive sets**: Komga stores tags unordered
      (a PATCH's array order is hash-scrambled server-side on storage, measured on
      ``gotson/komga:1.24.4``), so only a *content* change triggers a PATCH — the
      pushed list is still composed in canonical order (:func:`_compose_tags`).
    - ``links`` — upserts (:func:`_upsert_links`) the **"Book"** (= ``section_url``),
      **"Author"** (= ``author_url``) and **"ebookerr"** (``ebookerr_url``, built by
      the caller from ``app_external_url`` + the book id, omitted entirely when
      ``app_external_url`` is unset) links.

    Args:
        book: The merged book view to compare/push.
        current: The book's current Komga metadata (``GET`` response body).
        desired_tags: The canonical tag list from :func:`_compose_tags`.
        ebookerr_url: The "ebookerr" deep link to upsert, or ``None`` to omit it.

    Returns:
        A dict of only the changed fields; empty when nothing differs.
    """
    patch: dict[str, Any] = {}
    title = sanitize(book.title)
    if title and title != current.get("title"):
        patch["title"] = title
    summary = sanitize_multiline(book.synopsis) or sanitize_multiline(book.description) or ""
    if summary != (current.get("summary") or ""):
        patch["summary"] = summary
        logger.debug(
            'Komga summary sourced from %s for "%s"',
            "synopsis" if sanitize_multiline(book.synopsis) else "description",
            book.title,
        )
    # Komga's releaseDate is a date-only "YYYY-MM-DD" string, so the UTC datetime is
    # rendered down to its date before it is compared or pushed — comparing the datetime
    # itself would differ from Komga's string on every sync and patch it forever.
    release_date = book.date_published.date().isoformat() if book.date_published else None
    if release_date and release_date != current.get("releaseDate"):
        patch["releaseDate"] = release_date
    author = sanitize(book.author)
    if author:
        desired_authors = [{"name": author, "role": "writer"}]
        current_authors = [
            {"name": a.get("name"), "role": a.get("role")} for a in current.get("authors") or []
        ]
        if desired_authors != current_authors:
            patch["authors"] = desired_authors
    current_tags = [str(t) for t in current.get("tags") or []]
    if _ci_set(desired_tags) != _ci_set(current_tags):
        patch["tags"] = desired_tags
    desired_links: list[dict[str, str]] = [
        {"label": label, "url": url}
        for label, url in (
            ("Book", book.section_url),
            ("Author", book.author_url),
            ("ebookerr", ebookerr_url),
        )
        if url
    ]
    links, changed = _upsert_links(current.get("links") or [], desired_links)
    if changed:
        patch["links"] = links
    return patch


_STATUS_MAP: dict[str, str] = {"Completed": "ENDED", "In-Progress": "ONGOING"}


def _series_patch(book: BookView, current: dict[str, Any]) -> dict[str, Any]:
    """Changed-fields-only ``PATCH /api/v1/series/{id}/metadata`` body.

    ``current`` is the series' current Komga metadata (``GET /api/v1/series/{id}``,
    read by the caller). Fields, each included only when it differs from ``current``:

    - ``genres`` — :func:`_genres` (sanitised ``category`` split on ``", "``),
      compared case-insensitively as a set; overwritten wholesale when different.
    - ``status`` — ``sanitize(book.status)`` mapped through ``_STATUS_MAP``
      (``"Completed"`` → ``"ENDED"``, ``"In-Progress"`` → ``"ONGOING"``). Any value
      not in the map — ``None``, empty, or an unknown string like ``"Hiatus"`` — is
      omitted entirely (fail-closed; never clobbers manual curation in Komga).
    - ``publisher`` — ``sanitize(book.site)``; omitted when falsy.
    - ``links`` — upserts (:func:`_upsert_links`) the **"Story"** link only. The URL
      is ``series_url`` when the book belongs to a series (``book.series`` truthy;
      skipped entirely when ``series_url`` is unset) and ``story_url`` for a
      standalone book — chapter books sharing one Komga series must not fight over
      the series link with their own per-chapter URLs.

    Language is **not** synced (the local ``langcode``/``site_abbrev`` columns were
    dropped by a migration).

    Args:
        book: The merged book view to compare/push.
        current: The series' current Komga metadata (``GET`` response body).

    Returns:
        A dict of only the changed fields; empty when nothing differs.
    """
    patch: dict[str, Any] = {}
    genres = _genres(book)
    if _ci_set(genres) != _ci_set(current.get("genres") or []):
        patch["genres"] = genres
    mapped_status = _STATUS_MAP.get(sanitize(book.status) or "")
    if mapped_status is not None and mapped_status != current.get("status"):
        patch["status"] = mapped_status
    publisher = sanitize(book.site)
    if publisher and publisher != current.get("publisher"):
        patch["publisher"] = publisher
    link_url = book.series_url if book.series else book.story_url
    if link_url:
        links, changed = _upsert_links(
            current.get("links") or [], [{"label": "Story", "url": link_url}]
        )
        if changed:
            patch["links"] = links
    return patch


def _rating_from_tags(tags: list[Any]) -> int | None:
    """Return the first parseable rating (0–5) from tags, or None when absent/invalid.

    Recognises legacy ``rating:n`` and the canonical star form (e.g. ``★★★☆☆``).
    """
    for tag in tags:
        if isinstance(tag, str):
            val = _rating_from_tag(tag)
            if val is not None:
                return val
    return None


def _catalog_from_komga(
    komga_book: dict[str, Any], series: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """Recover ``(category, erotica_tags)`` from Komga — the inverse of the sync's tag push.

    ``category`` is the series genres rejoined (``["Het","Slash"]`` -> ``"Het, Slash"``);
    ``erotica_tags`` is the book tags with the genre values and any rating tag (legacy
    ``rating:*`` or star form) removed. The genre-tag comparison is case- and
    Unicode-form-insensitive (``TXE-D3``).

    Args:
        komga_book: The book's current Komga metadata (``GET`` response body).
        series: The book's current Komga series metadata, or ``None``.

    Returns:
        ``(category, erotica_tags)``, each ``None`` when empty.
    """
    genres = ((series or {}).get("metadata") or {}).get("genres") or []
    tags = (komga_book.get("metadata") or {}).get("tags") or []
    category = ", ".join(genres) or None
    drop = _ci_set(genres)
    erotica = [
        t
        for t in tags
        if isinstance(t, str) and not _is_rating_like(t) and nfc(t).casefold() not in drop
    ]
    return category, (", ".join(erotica) or None)


def _locations(progression: dict[str, Any]) -> Any:
    """Extract the ``locations`` dict from a Readium progression payload, or ``None``."""
    return (progression.get("locator") or {}).get("locations") if progression else None


def _href_basename(href: object) -> str | None:
    """Return a chapter href's basename, or ``None``."""
    if not isinstance(href, str) or not href:
        return None
    return href.rsplit("/", 1)[-1]


def _has_progress(progression: dict[str, Any]) -> bool:
    """True when a progression carries meaningful (non-zero) reading positions."""
    locations = _locations(progression) or {}
    return any(locations.get(key) for key in ("position", "progression", "totalProgression"))


def _chapter_table(
    positions: list[dict[str, Any]],
) -> tuple[list[str], list[str | None]]:
    """Ordered distinct hrefs (by locations.position) and each href's first non-empty title."""
    sorted_pos = sorted(positions, key=lambda p: (p.get("locations") or {}).get("position") or 0)
    hrefs: list[str] = []
    titles: dict[str, str | None] = {}
    for locator in sorted_pos:
        href = locator.get("href")
        if href and href not in hrefs:
            hrefs.append(href)
            title = locator.get("title")
            titles[href] = title if title else None
        elif href and href in hrefs:
            if href not in titles or not titles[href]:
                title = locator.get("title")
                if title:
                    titles[href] = title
    return hrefs, [titles.get(h) for h in hrefs]


class KomgaService:
    """Synchronise one downloaded book with Komga (see module docstring)."""

    def __init__(
        self,
        client: KomgaClient,
        *,
        server_url: str,
        enabled: bool,
        scan_retry_max: int,
        scan_retry_delay: float,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utc_now,
        external_url: str | None = None,
        app_external_url: str | None = None,
        library_id: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        library_folder: Path | None = None,
        link_owner: LinkOwnerLookup | None = None,
        circuit: CircuitGuard | None = None,
        scan_requests: ScanLedger | None = None,
    ) -> None:
        """Wire the Komga client and sync configuration.

        Args:
            client: The Komga REST client (``KomgaClient`` protocol).
            server_url: Komga base URL (docker-internal or localhost when not exposed).
            enabled: Whether Komga sync is turned on; ``sync``/``enrich`` short-circuit
                with a non-``ok`` result when ``False``.
            scan_retry_max: Maximum library-scan poll attempts when discovering a book.
            scan_retry_delay: Seconds between scan polls; a non-positive value floors
                to 2.0.
            sleep: Injectable sleep function (tests pass a no-op).
            now: Injectable UTC-now function (tests pass a fixed clock).
            external_url: Optional browser-facing Komga URL, used to build book deep links;
                when unset, falls back to ``server_url`` (``EXP-197``).
            app_external_url: This app's externally reachable base URL, used to build
                the "ebookerr" Komga link; ``None`` disables that link.
            library_id: The configured Komga library id, used to scope discovery/scan
                calls; ``None`` leaves them unscoped.
            monotonic: Injectable monotonic-clock function backing the connection-check
                TTL cache (tests pass a fake clock).
            library_folder: The library root EPUBs live under, used by :meth:`_epub_path_for`/
                :meth:`_document_hrefs` to open the book's own EPUB and build the anchor
                join's chapter table (``CHC-D12``); ``None`` leaves that document list
                unavailable and the join fails closed instead of fabricating one.
            link_owner: Answers "which book already holds this Komga id?" (``SPI 2.13``
                ``ctx.provider_link_owner``). ``None`` disables the exclusivity check. The
                service passes its own provider name, so a row linked to the other provider
                can never refuse this one.
            circuit: The app's shared circuit breakers (``SPI 2.21``). ``None`` keeps the
                pre-2.18.29 behaviour — a probe per call — and is only for a test that is not
                about reachability.
            scan_requests: The run's scan-request ledger, kept in the plugin's state
                (``DFT-D21``, ``PMG-D32``); ``None`` requests a scan for every changed file,
                as before.
        """
        self._client = client
        self._server_url = server_url
        self._external_url = external_url
        self._enabled = enabled
        self._scan_retry_max = scan_retry_max
        self._scan_retry_delay = scan_retry_delay if scan_retry_delay > 0 else 2.0
        self._sleep = sleep
        self._now = now
        self._app_external_url = app_external_url
        self._library_id = library_id
        self._monotonic = monotonic
        self._library_folder = library_folder
        self._link_owner = link_owner
        self._circuit = circuit
        self._scan_requests = scan_requests
        self._circuit_key = "provider:komga"
        self._circuit_label = "Komga"
        self._url_index: dict[str, str] | None = None  # url -> external_item_id; None = not fetched
        self._conn_ok_until: float = 0.0
        self._outage_logged = False
        self._pushed_series: set[str] = set()
        # Per-call memos, cleared at the top of _sync_reachable/_enrich_reachable. They exist so the
        # SPI 2.19 anchoring primitives cost no extra Komga round trip and so the semantic restore
        # can hand back the exact envelope it wrote.
        self._positions_cache: dict[str, list[dict[str, Any]]] = {}
        self._progression_cache: dict[str, dict[str, Any]] = {}
        self._written_progression: dict[str, dict[str, Any]] = {}

    def _connection_ok(self) -> bool:
        """test_connection() with a 60s success cache (per service instance).

        Failures are never cached, so an unreachable server is re-probed each call.
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
        network (``EXP-269``). The probe itself runs **inside** the guard, so a failing
        probe is the failure that opens the breaker.

        Args:
            book: The book about to be synced or enriched.

        Returns:
            A ``SyncResult`` to return immediately, or ``None`` when the call may proceed.
        """
        if self._circuit is not None and self._circuit.is_open(self._circuit_key):
            logger.debug(
                'Komga circuit is open — "%s" reported unreachable without a probe',
                book.title,
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
                'Komga is not reachable (%s) — sync skipped for "%s"; every later book in this '
                "batch is reported unreachable, not missing",
                detail,
                book.title,
            )
        else:
            logger.debug('Komga still not reachable — sync skipped for "%s"', book.title)
        return SyncResult(
            False,
            "Komga is not reachable",
            attempted=attempted,
            unreachable=True,
            fields={"external_chapter_count": None},
        )

    def _restore_semantic(
        self, komga_book_id: str, book: BookView, target: ReadPosition, join: AnchorJoin
    ) -> dict[str, Any]:
        """Deliver a pending semantic restore through the core anchoring orchestrator.

        The durable, cross-pull restore layer (``RP-PLUG-4``) — survives the EPUB
        having been replaced entirely, unlike the raw locator fast path
        (:meth:`_restore_progress_if_lost`), since a replaced EPUB gets new
        Komga-internal position ids on re-analyse. Runs instead of the raw path, for
        one sync, whenever :meth:`sync` receives a pending restore marker and the
        caller's join (built once in :meth:`_sync_reachable`, ``CHC-D12``) is consistent.

        The match, the fail-closed rule and the candidate/re-anchor log lines belong to
        :func:`~ebookerr_sdk.providers.anchoring.restore_to_target` (``RP-D9``) —
        this method's job is to hand it this service's ``place_bookmark`` primitive
        (``SPI 2.19``) through the pre-built join, turn the outcome back into the
        ``external_locator`` field the caller persists, and log the persisted envelope
        at DEBUG for anyone diffing what actually landed.

        Args:
            komga_book_id: The Komga book id to restore the position onto.
            book: The merged book view being restored (title, output filename).
            target: The stored position to restore.
            join: The anchor join, already computed from the provider's anchors and the
                book's chapter table (``CHC-D12``); the caller only reaches this method
                when ``join.consistent`` is ``True``.

        Returns:
            ``{"external_locator": <JSON of the progression envelope just written — modified,
            device, locator>}`` when the restore succeeds, or ``{}`` when no chapter matched
            or Komga rejected the write — preserving the locator only when the reader actually
            accepted the restore.
        """
        outcome = restore_to_target(
            self,
            komga_book_id,
            target=target,
            join=join,
            book_title=book.title,
            provider_name="Komga",
        )
        if not outcome.written:
            return {}
        written = self._written_progression.get(komga_book_id)
        if written is None:
            return {}
        logger.debug(
            'Persisted the restored progression envelope for "%s": href=%s, modified=%s',
            book.title,
            (written.get("locator") or {}).get("href"),
            written.get("modified"),
        )
        return {"external_locator": json.dumps(written)}

    def sync(  # noqa: C901
        self,
        book: BookView,
        *,
        allow_scan: bool = True,
        restore_target: ReadPosition | None = None,
    ) -> SyncResult:
        """Push local-mastered metadata to Komga and read reading state back.

        See the module docstring for the full numbered sequence. Makes no DB write
        itself — the caller persists ``SyncResult.fields``/``read_position``. On a
        first link to a Komga book that reports no reading progress, the local
        percent is backed up to RP history before the empty provider state is
        adopted (:meth:`_first_link_backup`, EXP-002); a Komga book that already
        carries progress still wins unconditionally, exactly as before.

        Args:
            book: The merged book view to sync.
            allow_scan: When ``False``, an unlinked/undiscoverable book returns a
                not-``ok`` result instead of triggering a library scan (the batch
                sync path).
            restore_target: A pending cross-pull restore marker. When set, the
                semantic restore layer runs instead of the raw locator fast path for
                this sync (see :meth:`_restore_semantic`).

        Returns:
            A :class:`SyncResult`. ``ok`` is ``False`` (never raises) when Komga is
            disabled/unreachable, the book has no title, or it never appears in
            Komga; the caller logs ``message`` and carries on. ``attempted`` is
            ``False`` for a disabled provider or when an open circuit breaker
            (``EXP-269``) skips the call from memory; ``True`` for every other
            outcome, including a probe or call that itself failed.
            ``restore_attempted`` is ``True`` only when a semantic restore was
            performed against the resolved Komga book; a caller consumes the
            one-shot restore marker on that flag, never on the marker's mere
            presence (``EXP-155``).
        """
        if not self._enabled:
            return SyncResult(False, "Komga sync is disabled", attempted=False)
        blocked = self._reachable_or_result(book)
        if blocked is not None:
            return blocked
        try:
            with self._guard():
                return self._sync_reachable(
                    book, allow_scan=allow_scan, restore_target=restore_target
                )
        except CircuitOpenError:
            return self._unreachable_result(book, "circuit open", attempted=False)
        except ProviderUnreachable as exc:
            return self._unreachable_result(book, str(exc))

    def _sync_reachable(  # noqa: C901
        self,
        book: BookView,
        *,
        allow_scan: bool = True,
        restore_target: ReadPosition | None = None,
    ) -> SyncResult:
        """Sync when provider is reachable (``EXP-192``).

        An already-linked book's stored id is re-validated against its own
        ``output_filename`` on every sync; a mismatch re-links and additionally clears
        ``external_locator``, because that locator was captured from the other book.

        With no pending restore marker, the core anchor check (``RP-D9``) runs first on
        every sync; the raw locator fast path (``RP-PLUG-4``) runs only when the core
        reports the bookmark still resolves or had nothing to decide. A finished book whose
        stored locator no longer resolves has that locator cleared (``RP-D9``) once, rather
        than being replayed or warned about on every sync. All of that — restore, re-anchor,
        raw fast path and capture alike — is skipped for this sync when Komga's chapter table
        does not describe the file on disk: a file-changed re-read that timed out, or an
        anchor join that is inconsistent outright (``CHC-D12``).

        Args:
            book: The merged book view to sync.
            allow_scan: When ``False``, return a not-ok result on discovery failure.
            restore_target: A pending cross-pull restore marker.

        Returns:
            A :class:`SyncResult`.
        """
        logger.debug(
            'Komga sync started for "%s" (book_id=%s, external_item_id=%s, allow_scan=%s)',
            book.title,
            book.book_id,
            book.external.item_id,
            allow_scan,
        )

        self._positions_cache.clear()
        self._progression_cache.clear()
        self._written_progression.clear()

        snapshot = (
            self._client.get_progression(book.external.item_id) if book.external.item_id else {}
        )

        resolved = self._ensure_present(book, allow_scan=allow_scan)
        if isinstance(resolved, LinkRefusal):
            return SyncResult(False, resolved.message, refused=True)
        if resolved is None:
            msg = (
                "book not found in Komga (scan skipped)"
                if not allow_scan
                else "book did not appear in Komga"
            )
            return SyncResult(False, msg, not_found=True)
        komga_book_id = resolved

        self._progression_cache[komga_book_id] = snapshot

        # The stored id only proves Komga still has *a* book under it (``book_exists`` answers
        # HTTP 200, not "this file"), so re-check it against this book's own path every sync.
        revalidated: str | LinkRefusal | None = None
        if book.external.item_id and komga_book_id == book.external.item_id:
            revalidated = self._revalidated_link(book)
        path_mismatch = isinstance(revalidated, str)
        stale_link_kept = (
            revalidated.stale_link_message if isinstance(revalidated, LinkRefusal) else None
        )
        if isinstance(revalidated, str):
            komga_book_id = revalidated

        relinked = bool(book.external.item_id) and komga_book_id != book.external.item_id
        if relinked:
            logger.info(
                'Komga id changed for "%s" (book_id=%s): %s -> %s',
                book.title,
                book.book_id,
                book.external.item_id,
                komga_book_id,
            )

        komga_book = self._client.get_book(komga_book_id)
        if komga_book is None:
            return SyncResult(False, "could not load the Komga book")
        series_id = komga_book.get("seriesId")

        stale = False
        if book.external.item_id and not relinked:
            nudged = self._nudge_if_file_changed(book)
            if nudged:
                info = self._wait_for_reread(
                    komga_book_id, book.file_size, book.title or "(unknown title)"
                )
                stale = info is None
                komga_book = info or komga_book

        fields: dict[str, Any] = {}
        current_tags = (komga_book.get("metadata") or {}).get("tags") or []
        komga_tag = _rating_from_tags(current_tags)
        local = book.rating
        desired_rating, adopt_n, do_clear = self._resolve_rating(local, komga_tag)

        imported = _komga_only_tags(current_tags, book.category, book.erotica_tags)
        erotica = book.erotica_tags
        if imported:
            erotica = ", ".join(_split_csv(book.erotica_tags) + imported) or None
            fields["erotica_tags"] = erotica
            logger.info(
                'Imported %d Komga tag(s) into "%s": %s',
                len(imported),
                book.title,
                ", ".join(imported),
            )
            logger.debug(
                'Komga tag import for "%s" patches erotica_tags only (category is never imported)',
                book.title,
            )
        desired_tags = _compose_tags(book.category, erotica, desired_rating)
        ebookerr_url = (
            f"{self._app_external_url.rstrip('/')}/books/{book.book_id}"
            if self._app_external_url
            else None
        )
        if ebookerr_url is None:
            logger.debug(
                "app_external_url not set; skipping the ebookerr link for %s", book.book_id
            )
        metadata_pushed = self._push_book_metadata(
            komga_book_id, book, komga_book, desired_tags, ebookerr_url
        )
        if series_id:
            self._push_series_metadata(series_id, book)

        if adopt_n is not None:
            logger.info('Adopted Komga rating %d for "%s"', adopt_n, book.title)
            fields["rating"] = adopt_n
            fields["external_progress_at"] = self._now()
        elif do_clear:
            fields["rating"] = None

        # Built once (CHC-D12): the restore/re-anchor branch below, the raw fast path's gate
        # and the joined chapter count all read this same join, so they can never disagree on
        # which of Komga's anchors is which of the book's own chapters.
        join = self._build_join(book, komga_book_id)
        # An empty anchor list is "nothing to check this run", not "stale" — reanchor_bookmark
        # and capture_position each already have their own not-applicable/no-anchors branch for
        # it (their own ladders' earlier step), so only a genuinely inconsistent non-empty join
        # skips read-position work here.
        stale = stale or (bool(join.anchors) and not join.consistent)
        if stale:
            logger.warning(
                'Read-position capture, re-anchor and restore skipped for "%s" (book_id=%s): '
                "Komga's chapter table does not describe the file on disk (%d anchor(s) name no "
                "document, %d chapter(s) have no anchor)",
                book.title,
                book.book_id,
                len(join.unknown_refs),
                len(join.unanchored_ordinals),
            )

        if path_mismatch:
            fields["external_locator"] = None
            logger.info(
                'Cleared the Komga locator for "%s" (book_id=%s): it was captured from book '
                "%s, which is not %r",
                book.title,
                book.book_id,
                book.external.item_id,
                book.output_filename,
            )
        restore_attempted = False
        restore_landed = False
        reanchor_written = False
        if stale:
            pass  # restore_attempted stays False so the one-shot marker survives (EXP-155).
        elif restore_target is not None:
            # Two-layer restore (RP-PLUG-4): the semantic restore (durable, cross-pull) replaces
            # the raw-locator fast path below for this run; the raw path still covers ordinary
            # syncs where an analyze wiped a position mid-sync.
            restored = self._restore_semantic(komga_book_id, book, restore_target, join)
            fields.update(restored)
            restore_attempted = True
            restore_landed = bool(restored)
        else:
            outcome = reanchor_bookmark(
                self,
                komga_book_id,
                book=book,
                stored=book.read_position,
                join=join,
                book_title=book.title,
                provider_name="Komga",
            )
            reanchor_written = outcome.written
            if outcome.written:
                written = self._written_progression.get(komga_book_id)
                if written is not None:
                    fields["external_locator"] = json.dumps(written)
            elif (
                outcome.action == REANCHOR_FINISHED
                and not outcome.resolves
                and book.progress.locator
            ):
                # RP-D9: a finished book is never re-anchored, so the reader is not moved — but the
                # locator names nothing the provider still has, and replaying or warning about it
                # every sync forever is noise. Forget it once, and say so once.
                fields["external_locator"] = None
                logger.info(
                    'Forgot the dead read-position locator for "%s" (book_id=%s): the book '
                    "reads as finished and the locator names nothing in the provider",
                    book.title,
                    book.book_id,
                )
            elif outcome.action in (REANCHOR_RESOLVED, REANCHOR_NOT_APPLICABLE):
                # The raw locator fast path (RP-PLUG-4) is kept, only gated: it runs when the
                # bookmark still resolves, or when the core had nothing to decide. It must never
                # run after the core wrote a correct bookmark, nor when the core has proven the
                # bookmark dead and unmatchable — replaying it is a guaranteed HTTP 400.
                snapshot = self._snapshot_with_db_fallback(
                    book, komga_book_id, snapshot, relinked=relinked and not path_mismatch
                )
                fields.update(self._restore_progress_if_lost(komga_book_id, snapshot, book))
        refreshed = self._client.get_book(komga_book_id) or komga_book
        # Repair Komga's page reset (RP-D17): when the re-anchor branch found nothing to write
        # and no semantic restore landed, check if Komga reset the page to 1 while keeping the
        # locator — re-PUT it with a fresh modified timestamp so Komga recomputes the page.
        if not stale and not restore_landed and not reanchor_written:
            refreshed = self._repair_page_reset(komga_book_id, refreshed, book)
        read_back_fields = self._read_back(book, refreshed)
        fields.update(read_back_fields)

        fields["external_item_id"] = komga_book_id
        fields["external_collection_id"] = series_id
        fields["external_provider"] = "komga"
        fields["external_library_id"] = self._library_id
        fields["external_synced_at"] = self._now()
        base = deep_link_base(self._server_url, self._external_url)
        fields["external_item_url"] = f"{base}/book/{komga_book_id}"

        fields["external_chapter_count"] = join.chapter_anchor_count
        logger.info(
            'Komga reports %d chapter(s) for "%s" (item_id=%s); ebookerr packages %s',
            join.chapter_anchor_count,
            book.title,
            komga_book_id,
            book.num_chapters,
        )

        if stale:
            read_position: ReadPosition | None = None
            progression: dict[str, Any] = {}
        else:
            read_position, progression = self._semantic_position(
                komga_book_id, refreshed, book, join
            )
        positions = self._positions_for(komga_book_id)
        backward = self._warn_on_backward_move(
            book, read_position, progression, len(positions), restore_attempted=restore_attempted
        )
        if read_position is None:
            read_position = self._first_link_backup(book, komga_book_id, refreshed)
        return SyncResult(
            True,
            "synced",
            current_page=read_back_fields["external_read_position"],
            total_pages=read_back_fields["external_read_total"],
            fields=fields,
            read_position=read_position,
            metadata_pushed=metadata_pushed,
            backward_move=backward,
            restore_attempted=restore_attempted,
            restore_landed=restore_landed,
            relinked=path_mismatch,
            stale_link_kept=stale_link_kept,
        )

    def _nudge_if_file_changed(self, book: BookView) -> bool:
        """Ask Komga to scan the book's library when its file changed since the last link attempt.

        Komga never re-reads a rewritten file on its own (a library with scans disabled serves the
        old chapters forever), so the sync that observes the size mismatch asks for the scan; a
        caller that gets ``True`` back waits for it to land (:meth:`_wait_for_reread`, ``EXP-202``,
        ``CHC-D12``).

        Args:
            book: The linked book being synced.

        Returns:
            ``True`` only when the file changed, a Komga library id was on record, and either
            a previous scan request already covers the change or Komga accepted a new scan
            request — the one outcome worth waiting on. ``False`` otherwise (nothing changed,
            no library id to scan, or Komga refused the request) (``DFT-D21``).
        """
        if not file_changed_since_attempt(
            link_attempted_at=book.external.link_attempted_at,
            link_attempt_size=book.external.link_attempt_size,
            file_size=book.file_size,
        ):
            return False
        library_id = book.external.library_id or self._library_id
        if not library_id:
            logger.debug(
                'Library-scan nudge skipped for "%s" (book_id=%s): no Komga library id on record',
                book.title,
                book.book_id,
            )
            return False
        ledger_key = f"{self._server_url}|library:{library_id}"
        changed_at = (
            file_changed_at(self._library_folder / book.output_filename)
            if self._library_folder is not None and book.output_filename
            else None
        )
        if (
            self._scan_requests is not None
            and changed_at is not None
            and self._scan_requests.covers(ledger_key, changed_at)
        ):
            logger.debug(
                'Library-scan nudge skipped for "%s" (book_id=%s): library %s was already asked to '
                "scan after the file changed",
                book.title,
                book.book_id,
                library_id,
            )
            return True
        requested_at = self._scan_requests.now() if self._scan_requests is not None else 0.0
        if self._client.scan_library(str(library_id)):
            if self._scan_requests is not None:
                self._scan_requests.record(ledger_key, requested_at)
            logger.info(
                'Asked Komga to scan library %s for "%s" (file changed)',
                library_id,
                book.title,
            )
            return True
        logger.warning(
            'Komga did not accept the library scan %s for "%s"',
            library_id,
            book.title,
        )
        return False

    def _wait_for_reread(
        self, komga_book_id: str, expected_size: int | None, title: str
    ) -> dict[str, Any] | None:
        """Poll Komga until it reports having re-read a just-nudged file (``EXP-202``, ``CHC-D12``).

        Called only right after a scan was requested for a file-size change; a re-read Komga has
        not yet applied still serves the old chapters, so read-position work against it would
        name the wrong chapter, or overwrite a live bookmark with a write aimed at the wrong one.

        Args:
            komga_book_id: The Komga book id to poll.
            expected_size: The library's freshly-measured file size (``BookView.file_size``), or
                ``None`` when the library has never measured it.
            title: The book's user-facing title, used only in log lines.

        Returns:
            The fresh ``get_book`` payload once its ``sizeBytes`` matches ``expected_size``, having
            cleared this instance's per-call position/progression memos for ``komga_book_id`` so
            the rest of the sync reads them fresh; ``None`` when ``expected_size`` is unknown, or
            the poll budget (``scan_retry_max`` polls, ``scan_retry_delay`` apart) is exhausted
            first.
        """
        if expected_size is None:
            logger.debug('No file size on record for "%s"; not waiting for Komga', title)
            return None
        for poll in range(1, self._scan_retry_max + 1):
            self._sleep(self._scan_retry_delay)
            info = self._client.get_book(komga_book_id)
            if info is not None and int(info.get("sizeBytes") or -1) == expected_size:
                logger.info(
                    'Komga re-read "%s" (item_id=%s) after %d poll(s)', title, komga_book_id, poll
                )
                self._positions_cache.pop(komga_book_id, None)
                self._progression_cache.pop(komga_book_id, None)
                return info
        logger.warning(
            'Komga has not re-read "%s" (item_id=%s) after %d poll(s); read-position work waits '
            "for the next sync",
            title,
            komga_book_id,
            self._scan_retry_max,
        )
        return None

    def _document_hrefs(self, book: BookView) -> tuple[str, ...] | None:
        """The book's current EPUB reading-order document hrefs, for the anchor join (``CHC-D12``).

        Args:
            book: The merged book view whose EPUB to read.

        Returns:
            :func:`~ebookerr_sdk.epub.chapters.spine_document_hrefs`, or ``None`` when the
            library path is unknown or the EPUB cannot be opened — :func:`join_anchors` then
            fails closed on any anchor that does not name one of the book's own chapters,
            rather than guessing it is a harmless non-chapter document.
        """
        from ebookerr_sdk.epub.chapters import spine_document_hrefs
        from ebookerr_sdk.epub.document import EpubDocument

        path = self._epub_path_for(book)
        if path is None:
            logger.debug(
                'No EPUB path on record for "%s"; the anchor join cannot verify non-chapter '
                "documents",
                book.title,
            )
            return None
        try:
            return spine_document_hrefs(EpubDocument.open(path))
        except Exception:  # noqa: BLE001 — fail soft: an unreadable EPUB must not fail the sync
            logger.debug(
                'Could not read the EPUB for "%s" at %s; the anchor join cannot verify '
                "non-chapter documents",
                book.title,
                path,
            )
            return None

    def _build_join(self, book: BookView, komga_book_id: str) -> AnchorJoin:
        """Join Komga's anchors onto the book's own chapter table (``CHC-D12``).

        The one place :func:`~ebookerr_sdk.providers.anchoring.join_anchors` is called —
        :meth:`_sync_reachable`'s read-position work and :meth:`_enrich_reachable`'s read-only
        capture both join the same way, so they can never disagree on which of Komga's anchors
        is which of the book's own chapters.

        Args:
            book: The merged book view whose chapter table and EPUB to join against.
            komga_book_id: The Komga book id whose anchors to list.

        Returns:
            The computed ``AnchorJoin``.
        """
        return join_anchors(
            self.list_anchors(komga_book_id),
            book.chapter_table,
            self._document_hrefs(book),
            book_title=book.title,
        )

    def sync_batch(
        self,
        books: list[BookView],
        *,
        allow_scan: bool = True,
    ) -> list[SyncResult]:
        """Sync multiple books and log a summary of the run.

        Processes each book via :meth:`sync`, maintaining counters for updated
        (books whose metadata was PATCHed), unchanged (books with no differences),
        and failed (books whose sync did not complete). Logs a single INFO summary
        at the end with all counts and the elapsed wall-clock time.

        Args:
            books: The books to sync.
            allow_scan: When ``False``, unlinked/undiscoverable books return a
                not-``ok`` result instead of triggering a library scan.

        Returns:
            One ``SyncResult`` per input book, in the same order.
        """
        start = time.monotonic()
        total = len(books)
        updated = 0
        unchanged = 0
        failed = 0
        results: list[SyncResult] = []

        for book in books:
            result = self.sync(book, allow_scan=allow_scan)
            results.append(result)

            if not result.ok:
                failed += 1
            elif result.metadata_pushed:
                updated += 1
            else:
                unchanged += 1

        logger.info(
            "Komga sync finished: %d book(s) — %d updated, %d unchanged, %d failed in %.1fs",
            total,
            updated,
            unchanged,
            failed,
            time.monotonic() - start,
        )

        return results

    @staticmethod
    def _resolve_rating(
        local: int | None, komga_tag: int | None
    ) -> tuple[int | None, int | None, bool]:
        """Rating decision table (behavioural contract): ebookerr wins conflicts.

        ``local`` = ``books.rating``; ``komga_tag`` = :func:`_rating_from_tags` on the
        book's current Komga tags. Tag format: a **five-character star string** — n
        ``★`` (U+2605) followed by (5−n) ``☆`` (U+2606), e.g. ``★★★☆☆`` for 3.
        ``NULL``/``0`` = no tag (removed on push). Three read forms are recognised:
        (1) the canonical 5-char star string; (2) 1–5 ``★`` alone (hand-typed
        tolerance, e.g. ``★★★`` → 3); (3) legacy ``rating:n`` (n ∈ 0–5), replaced by
        the star form on the next push. ``NULL`` = unrated, ``0`` = cleared (pending
        deletion), 1–5 = score.

        | local | Komga tag | desired_rating | outcome |
        | --- | --- | --- | --- |
        | NULL | none | None | never create |
        | NULL | 1–5 (n) | n | adopt: DB rating <- n, bumps external_progress_at |
        | NULL | 0 | None | strips the tag on Komga; no DB change |
        | 0 | any/none | None | clear: DB rating <- NULL |
        | 1–5 | any/none | local | local wins — overwrites a differing Komga tag |

        The ``0``-clear is applied by the caller only *after* the metadata push
        (:meth:`sync` sends the tags PATCH stripping the star tag before acting on
        ``do_clear``); if Komga is unreachable, ``sync`` returns before any of this
        runs, so a pending clear survives untouched for the next attempt. Rating
        adoption counts as a reading-state change and bumps ``external_progress_at``.

        Args:
            local: The book's current local rating (``books.rating``): ``None``
                (unrated), ``0`` (pending clear), or 1–5.
            komga_tag: The rating parsed from Komga's current tags, or ``None``.

        Returns:
            ``(desired_rating, adopt_n, do_clear)`` — ``desired_rating`` is the value
            for :func:`_compose_tags` (1–5 → star tag, ``None`` → omitted); ``adopt_n``
            is the rating to write to the DB from Komga (adopt path only); ``do_clear``
            is ``True`` when ``local == 0`` and the pending clear must flip to ``NULL``
            after the push.
        """
        if local is None:
            if komga_tag is not None and 1 <= komga_tag <= 5:
                return komga_tag, komga_tag, False  # adopt: keep tag, write to DB
            return None, None, False  # no-create (komga_tag is None or 0)
        if local == 0:
            return None, None, True  # clear: strip tag, flip DB to NULL after push
        return local, None, False  # push 1–5: lit wins

    def enrich(self, book: BookView) -> SyncResult:
        """Read-only Komga lookup for a scanned book (F11): ids + reading state + catalog.

        Tries path-first (T7): suffix-match ``output_filename`` against the library URL index;
        falls back to ``find_book_id(title, author)`` on a miss. No metadata push, no
        scan/analyze, no progression write. Returns a not-``ok`` result when Komga is
        disabled/unreachable or the book is not found in Komga. ``attempted`` is ``True``
        for every outcome except when Komga is disabled or an open circuit breaker
        (``EXP-269``) answers unreachable from memory instead of probing.
        A book another row already owns is refused with its owner named (``EXP-187``).
        Never attempts a restore, so its result always reports ``restore_attempted=False``.
        """
        if not self._enabled:
            return SyncResult(False, "Komga sync is disabled", attempted=False)
        blocked = self._reachable_or_result(book)
        if blocked is not None:
            return blocked
        try:
            with self._guard():
                return self._enrich_reachable(book)
        except CircuitOpenError:
            return self._unreachable_result(book, "circuit open", attempted=False)
        except ProviderUnreachable as exc:
            return self._unreachable_result(book, str(exc))

    def _enrich_reachable(self, book: BookView) -> SyncResult:
        """Read-only lookup when provider is reachable (``EXP-192``).

        Args:
            book: The merged book view to look up.

        Returns:
            A :class:`SyncResult`.
        """
        self._positions_cache.clear()
        self._progression_cache.clear()
        self._written_progression.clear()

        komga_book_id = self._find_by_path(book.output_filename)
        by_path = komga_book_id is not None
        if not komga_book_id:
            if not book.title:
                return SyncResult(False, "no title", attempted=True)
            komga_book_id = self._client.find_book_id(book.title, book.author)
        if not komga_book_id:
            return SyncResult(False, "book not found in Komga", attempted=True, not_found=True)
        refusal = self._refusal_for(komga_book_id, book, "path" if by_path else "title")
        if refusal is not None:
            return SyncResult(False, refusal.message, attempted=True, refused=True)
        komga_book = self._client.get_book(komga_book_id)
        if komga_book is None:
            return SyncResult(False, "could not load the Komga book", attempted=True)
        series_id = komga_book.get("seriesId")
        series = self._client.get_series(series_id) if series_id else None
        category, erotica_tags = _catalog_from_komga(komga_book, series)
        fields = self._read_back(book, komga_book)
        # T8: title-page catalog (T4) is authoritative; only fill when the book lacks it.
        if (category is not None or erotica_tags is not None) and (
            book.category is None and book.erotica_tags is None
        ):
            fields["category"] = category
            fields["erotica_tags"] = erotica_tags
        # Adopt rating from Komga only when the local rating is unset (read-only toward Komga).
        if book.rating is None:
            current_tags = (komga_book.get("metadata") or {}).get("tags") or []
            komga_tag = _rating_from_tags(current_tags)
            if komga_tag is not None and 1 <= komga_tag <= 5:
                fields["rating"] = komga_tag
        fields["external_item_id"] = komga_book_id
        fields["external_collection_id"] = series_id
        fields["external_provider"] = "komga"
        fields["external_library_id"] = self._library_id
        fields["external_synced_at"] = self._now()
        join = self._build_join(book, komga_book_id)
        read_position, _ = self._semantic_position(komga_book_id, komga_book, book, join)
        return SyncResult(True, "enriched", fields=fields, read_position=read_position)

    def _find_by_path(self, output_filename: str | None) -> str | None:
        """Locate a Komga book by URL suffix-match against ``output_filename``.

        Both sides are compared in Unicode Normalization Form C, so a decomposed Komga
        url (macOS/SMB) matches a composed ``output_filename`` (``TXE-D3``). Returns
        ``None`` when nothing matches or more than one book does.
        """
        if not output_filename:
            return None
        index = self._ensure_url_index()
        suffix = "/" + nfc(output_filename)
        matches = [bid for url, bid in index.items() if nfc(url).endswith(suffix)]
        if len(matches) > 1:
            logger.warning(
                "Komga path %r matches %d books (%s) — refusing to guess; "
                "resolve the duplicate in Komga",
                output_filename,
                len(matches),
                ", ".join(sorted(matches)),
            )
            return None
        return matches[0] if matches else None

    def _revalidated_link(self, book: BookView) -> str | LinkRefusal | None:
        """The Komga id this book's own file is at, when it differs from the stored one.

        Answers ``None`` — keep the stored link — for every uncertain case: the book has no
        ``output_filename``, the library URL index is empty (no library id configured, or the
        listing failed), the index does not list that path, or the index agrees with the
        stored id. Only a positive, different answer re-links, and only when no other library
        row owns it (``EXP-149``).

        Args:
            book: The linked book being synced.

        Returns:
            The differing Komga book id; a :class:`LinkRefusal` when that id belongs to
            another library row, in which case the caller keeps the stale link; or ``None``
            to keep the stored link.
        """
        if not book.output_filename:
            return None
        index = self._ensure_url_index()
        if not index:
            return None
        found = self._find_by_path(book.output_filename)
        if found is None:
            logger.debug(
                'Komga path re-validation kept the stored link for "%s": %r is not in the '
                "Komga library index (%d book(s))",
                book.title,
                book.output_filename,
                len(index),
            )
            return None
        if found == book.external.item_id:
            return None
        if self._link_owner is not None:
            owner = self._link_owner(found, provider="komga")
            if owner is not None and owner != book.book_id:
                logger.warning(
                    'Komga link for "%s" (book_id=%s) should move to book %s (%r), but '
                    "book_id=%s already owns it — keeping the stale link %s",
                    book.title,
                    book.book_id,
                    found,
                    book.output_filename,
                    owner,
                    book.external.item_id,
                )
                return LinkRefusal(
                    provider="Komga", noun="book", item_id=found, owner_book_id=owner
                )
        logger.warning(
            'Komga link for "%s" (book_id=%s) pointed at book %s, but %r is Komga book %s '
            "— re-linking",
            book.title,
            book.book_id,
            book.external.item_id,
            book.output_filename,
            found,
        )
        return found

    def _ensure_url_index(self) -> dict[str, str]:
        """Lazily build and cache the url -> book_id index from the Komga library (T7).

        Logs the resulting size once per build.
        """
        if self._url_index is None:
            pairs = self._client.list_library_books()
            self._url_index = {url: bid for bid, url in pairs}
            logger.debug("Built the Komga library URL index: %d book(s)", len(self._url_index))
        return self._url_index

    def _refusal_for(self, komga_book_id: str, book: BookView, how: str) -> LinkRefusal | None:
        """Return the refusal when another row owns ``komga_book_id`` (``EXP-149``, ``EXP-187``).

        Args:
            komga_book_id: The id discovery just resolved.
            book: The book trying to claim it.
            how: How it was resolved (``"title"`` or ``"path"``), for the log line.

        Returns:
            ``None`` when the id is unclaimed or already this book's; a :class:`LinkRefusal`
            when a different book owns it, having logged one WARNING naming both.
        """
        if self._link_owner is None:
            return None
        owner = self._link_owner(komga_book_id, provider="komga")
        if owner is None or owner == book.book_id:
            return None
        logger.warning(
            'Refused to link "%s" (book_id=%s) to Komga book %s found by %s: '
            "book_id=%s already owns it — leaving this book unlinked",
            book.title,
            book.book_id,
            komga_book_id,
            how,
            owner,
        )
        return LinkRefusal(
            provider="Komga", noun="book", item_id=komga_book_id, owner_book_id=owner
        )

    def _ensure_present(  # noqa: C901
        self, book: BookView, *, allow_scan: bool = True, analyze: bool = False
    ) -> str | LinkRefusal | None:
        """Resolve the book's Komga id, discovering and scanning if necessary.

        Already linked -> return the known id, optionally triggering ``analyze``, unless
        ``book_exists`` returns ``False`` (HTTP 404), in which case fall through to
        rediscovery. An indeterminate ``None`` (server down / 5xx) deliberately keeps
        the current link. Otherwise, discovery is **path-first**: (1)
        ``_find_by_path(output_filename)`` — the file path is correct the moment a scan
        finishes; (2) ``find_book_id(title, author)`` — the fallback for a book whose
        file Komga has not indexed under the expected path, which needs Komga's
        asynchronously-populated author metadata. If still not found and ``allow_scan``
        is ``True``, trigger a **library scan** and poll up to ``scan_retry_max`` times
        (``scan_retry_delay`` apart), invalidating the cached URL index each attempt and
        retrying path-then-title.

        Analyze is off by default because Komga's ``analyze`` re-imports the EPUB's
        ``dc:description`` metadata into the book's ``summary``, which would cause
        every sync to detect a summary change and push it back, overwriting the local
        value repeatedly.

        Args:
            book: The merged book view to locate in Komga.
            allow_scan: When ``False``, return ``None`` immediately on a miss instead
                of scanning (the batch-sync path).
            analyze: When ``True``, trigger Komga's analyze for an already-linked book.
                Defaults to ``False`` to avoid the metadata reimport cycle described above.

        Returns:
            The Komga book id, or ``None`` when the book is not found — the path index
            does not list its ``output_filename``, and it has no title to fall back on
            or the title lookup missed, and scanning is disallowed or exhausts its retries.
            A :class:`LinkRefusal` the moment a discovered id turns out to be owned by
            another row — returned **before** any library scan or poll, so the refusal
            is logged exactly once per sync (``EXP-187``).
            Title+author is kept as the second key for the cases path cannot serve: no Komga
            library id configured (the URL index is then permanently empty), and the window
            between an EPUB rename and Komga's next scan.
        """
        if book.external.item_id:
            if self._client.book_exists(book.external.item_id) is False:
                # Komga re-issues ids when it re-indexes a file; fall through to rediscovery.
                # A None answer (server down / 5xx) deliberately keeps the current link.
                logger.warning(
                    'Komga book id %s for "%s" no longer exists — re-resolving the link',
                    book.external.item_id,
                    book.title,
                )
            else:
                if analyze:
                    logger.debug(
                        'Komga book already linked for "%s"; triggering analyze', book.title
                    )
                    self._client.trigger_analyze(book.external.item_id)
                else:
                    logger.debug('Komga book already linked for "%s"', book.title)
                return book.external.item_id
        found = self._find_by_path(book.output_filename)
        if found:
            refusal = self._refusal_for(found, book, "path")
            if refusal is not None:
                return refusal
            logger.debug('Found "%s" in Komga by path', book.title)
            return found
        # Komga's file paths are correct the moment a scan finishes; its author metadata is
        # populated asynchronously, so title+author is the weaker, second key.
        if not book.title:
            return None
        logger.debug(
            'Komga path lookup missed for "%s" (%r); falling back to title+author',
            book.title,
            book.output_filename,
        )
        found = self._client.find_book_id(book.title, book.author)
        if found:
            refusal = self._refusal_for(found, book, "title")
            if refusal is not None:
                return refusal
            logger.debug('Found "%s" in Komga by title', book.title)
            return found
        if not allow_scan:
            return None
        logger.info('Triggering Komga library scan to locate "%s"', book.title)
        self._client.trigger_library_scan()
        for _poll in range(self._scan_retry_max):
            self._sleep(self._scan_retry_delay)
            self._url_index = None  # force index rebuild; path is reliable before author metadata
            found = self._find_by_path(book.output_filename)
            if found:
                refusal = self._refusal_for(found, book, "path")
                if refusal is not None:
                    return refusal
                return found
            found = self._client.find_book_id(book.title, book.author)
            if found:
                refusal = self._refusal_for(found, book, "title")
                if refusal is not None:
                    return refusal
                return found
        logger.warning(
            '"%s" did not appear in Komga after %d scan poll(s)', book.title, self._scan_retry_max
        )
        return None

    def delete_remote_book(self, external_item_id: str) -> None:
        """Remove a book from Komga: delete its file, then scan and empty the trash.

        Called from ``KomgaSyncPlugin.enrich`` on a ``BookDeleted`` event, once per
        deleted book in that event's batch, after the local delete has already
        happened. Best-effort and **never raises** — a Komga-side failure must not
        surface as an app error:

        1. ``get_book`` — if Komga already has no record, log and return (nothing to
           delete).
        2. Read ``library_id`` off **that book's own** ``libraryId`` (not the globally
           configured library setting — the book may live in a different library) and
           ``delete_book_file`` to remove it from the shared volume.
        3. No resolvable ``library_id`` -> log a warning and stop (scan/empty-trash
           both need one).
        4. ``scan_library``, then poll ``get_book`` (up to ``scan_retry_max`` times,
           ``scan_retry_delay`` apart) until it returns ``None``; still present after
           every retry logs a warning but the flow proceeds anyway — a stuck scan
           shouldn't block trash cleanup.
        5. ``empty_trash_for`` — purges the now-orphaned entry so it disappears from
           Komga entirely instead of lingering as a "missing file" placeholder.

        Each step logs ``ok``/``FAILED`` at INFO/WARNING, so a partial failure is
        visible on the Logs page without blocking the rest of the chain. The whole
        sequence runs inside the shared circuit breaker (``EXP-269``): once it is
        open, a later book in the same delete batch is skipped without a probe
        instead of paying its own timeout.

        Args:
            external_item_id: The Komga book id to delete.
        """
        try:
            with self._guard():
                book = self._client.get_book(external_item_id)
                if book is None:
                    logger.info(
                        "Komga book %s already absent — nothing to delete", external_item_id
                    )
                    return
                library_id = str(book.get("libraryId") or "")
                deleted = self._client.delete_book_file(external_item_id)
                logger.info(
                    "Komga delete requested for book %s (library=%s): %s",
                    external_item_id,
                    library_id or "unknown",
                    "ok" if deleted else "FAILED",
                )
                if not library_id:
                    logger.warning(
                        "Komga book %s has no libraryId — cannot scan/empty trash",
                        external_item_id,
                    )
                    return
                self._client.scan_library(library_id)
                for _ in range(self._scan_retry_max):
                    if self._client.get_book(external_item_id) is None:
                        break
                    time.sleep(self._scan_retry_delay)
                else:
                    logger.warning(
                        "Komga book %s still present after %d poll(s) — emptying trash anyway",
                        external_item_id,
                        self._scan_retry_max,
                    )
                emptied = self._client.empty_trash_for(library_id)
                logger.info(
                    "Komga trash emptied for library %s: %s",
                    library_id,
                    "ok" if emptied else "FAILED",
                )
        except CircuitOpenError:
            logger.debug(
                "Komga circuit is open — delete skipped for book %s without a probe",
                external_item_id,
            )
        except ProviderUnreachable as exc:
            logger.warning(
                "Komga is not reachable (%s) — could not delete book %s", exc, external_item_id
            )

    def _push_book_metadata(
        self,
        komga_book_id: str,
        book: BookView,
        komga_book: dict[str, Any],
        desired_tags: list[str],
        ebookerr_url: str | None = None,
    ) -> bool:
        """Build and push the book's changed-fields-only metadata PATCH.

        See :func:`_book_patch` for the field rules. No-op (DEBUG log only) when
        nothing differs.

        Args:
            komga_book_id: The Komga book id to PATCH.
            book: The merged book view to compare/push.
            komga_book: The book's current Komga record (``GET`` response body).
            desired_tags: The canonical tag list from :func:`_compose_tags`.
            ebookerr_url: The "ebookerr" deep link to upsert, or ``None`` to omit it.

        Returns:
            ``True`` when a PATCH was sent, ``False`` when nothing differed.
        """
        patch = _book_patch(book, komga_book.get("metadata") or {}, desired_tags, ebookerr_url)
        if patch:
            logger.info(
                'Pushing Komga book metadata for "%s": %s',
                book.title,
                ", ".join(sorted(patch)),
            )
            self._client.patch_book_metadata(komga_book_id, patch)
            return True
        else:
            logger.debug('Komga book metadata unchanged for "%s"', book.title)
            return False

    def _push_series_metadata(self, series_id: str, book: BookView) -> None:
        """Build and push the series' changed-fields-only metadata PATCH.

        See :func:`_series_patch` for the field rules. Convergence guard: pushes at
        most once per ``series_id`` per ``KomgaService`` instance
        (``_pushed_series`` — first sibling book in a batched run wins; the set spans
        one batched call and resets when the service is rebuilt). No-op when the
        series can't be loaded or nothing differs.

        Args:
            series_id: The Komga series id to PATCH.
            book: The merged book view whose series-level fields to push.
        """
        if series_id in self._pushed_series:
            logger.debug('Komga series metadata already handled this run for "%s"', book.title)
            return
        self._pushed_series.add(series_id)
        series = self._client.get_series(series_id)
        if series is None:
            return
        patch = _series_patch(book, series.get("metadata") or {})
        if patch:
            logger.info(
                'Pushing Komga series metadata for "%s": %s',
                book.title,
                ", ".join(sorted(patch)),
            )
            self._client.patch_series_metadata(series_id, patch)
        else:
            logger.debug('Komga series metadata unchanged for "%s"', book.title)

    def _read_back(self, book: BookView, komga_book: dict[str, Any]) -> dict[str, Any]:
        """Read Komga-mastered reading state back: page count, position, completion.

        ``external_progress_at`` is stamped when the read position or completion changed,
        or when the page total changed and Komga reported the previous total
        (``external_provider == "komga"``). A changed page total from another provider or on
        first link is re-baselined silently (``EXP-195``). It tracks *changes* to the
        Komga-mastered reading state (rating adoption also bumps it, see :meth:`sync`).
        It is DB-internal only; never shown in the UI.

        Args:
            book: The merged book view, for its current read-progress values.
            komga_book: The book's current Komga record (``GET`` response body).

        Returns:
            ``external_read_total``/``_position``/``_completed``/``_percent``, plus
            ``external_progress_at`` when something changed, and ``read_completed_at``
            when Komga reports a completion with a valid date.
        """
        media = komga_book.get("media") or {}
        progress = komga_book.get("readProgress") or {}
        page_count = int(media.get("pagesCount") or 0)
        read_position = int(progress.get("page") or 0)
        completed = 1 if progress.get("completed") else 0
        read_percent = read_position / page_count if page_count > 0 else 0.0
        fields: dict[str, Any] = {
            "external_read_total": page_count,
            "external_read_position": read_position,
            "external_read_completed": completed,
            "external_read_percent": read_percent,
        }
        # Komga knows when the book was actually finished; ebookerr would otherwise record
        # "when it noticed" (plugin_runtime falls back to now()). Written on every sync, so a
        # library whose dates were approximated by migration 42 repairs itself on the next one.
        if completed:
            read_date = parse_datetime(progress.get("readDate"))
            if read_date is not None:
                fields["read_completed_at"] = read_date
            else:
                logger.debug(
                    'Komga reported "%s" completed with no usable readDate (%r)',
                    book.title,
                    progress.get("readDate"),
                )
        same_provider = book.external.provider == "komga"
        if (
            read_position != book.progress.position
            or completed != book.progress.completed
            or (same_provider and page_count != book.progress.total)
        ):
            fields["external_progress_at"] = self._now()
        return fields

    def _warn_on_backward_move(
        self,
        book: BookView,
        read_position: ReadPosition | None,
        progression: dict[str, Any],
        positions_count: int,
        *,
        restore_attempted: bool = False,
    ) -> str | None:
        """Warn when a read position moves backwards, and return a user-safe summary (``RP-D18``).

        An empty provider read-back is never a backward move: the provider is reporting that it
        has no position, which the apply path never records (``RP-CAP-3``).
        When ``restore_attempted`` is True and the move is backwards: INFO log only, no user
        message, so no durable notice is created.

        Args:
            book: The merged book view (for prior stored position and title).
            read_position: The newly-captured semantic read position, or ``None``.
            progression: The raw Komga progression dict (carries device/modified info).
            positions_count: The count of positions in Komga's table.
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
        if read_position is None or not moves_backwards(prev, read_position):
            return None
        if prev is None:
            raise RuntimeError("moves_backwards returned True with prev=None")
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
        device = progression.get("device") or {}
        logger.warning(
            'Read position for "%s" moved backwards: chapter_index %d -> %d, %.0f%% -> %.0f%% '
            "(written by device=%s name=%s at %s; %d positions in Komga's table)",
            book.title,
            prev.chapter_index,
            read_position.chapter_index,
            prev.chapter_progress * 100,
            read_position.chapter_progress * 100,
            device.get("id"),
            device.get("name"),
            progression.get("modified"),
            positions_count,
        )
        return backward_move_message(book.title, prev, read_position)

    def _positions_for(self, item_id: str) -> list[dict[str, Any]]:
        """This call's Komga positions table, fetched at most once per sync or enrich."""
        cached = self._positions_cache.get(item_id)
        if cached is None:
            cached = self._client.get_positions(item_id)
            self._positions_cache[item_id] = cached
        return cached

    def list_anchors(self, item_id: str) -> list[ProviderAnchor]:
        """List resolvable chapter anchors for a book (``SPI 2.19`` ``ReadPositionAnchoring``).

        Implements the anchoring contract. Each anchor's ``ref`` is the position table's
        full href (e.g., ``"OEBPS/chapter_01_cookie.xhtml"``).

        Args:
            item_id: The Komga book id.

        Returns:
            List of ``ProviderAnchor`` ordered by position, one per distinct href.
        """
        hrefs, titles = _chapter_table(self._positions_for(item_id))
        return [
            ProviderAnchor(ref=href, title=title, ordinal=ordinal)
            for ordinal, (href, title) in enumerate(zip(hrefs, titles, strict=True))
        ]

    def read_bookmark(self, item_id: str) -> ProviderBookmark | None:
        """Read the provider's stored bookmark (``SPI 2.19`` ``ReadPositionAnchoring``).

        Implements the anchoring contract. Returns the parsed bookmark, or ``None``
        when Komga's progression carries no locator href at all. A bookmark whose
        ``ref`` the current anchor list no longer contains is a *dead* bookmark and
        is returned normally.

        Args:
            item_id: The Komga book id.

        Returns:
            The parsed ``ProviderBookmark``, or ``None`` when no href is available.
        """
        progression = self._progression_cache.get(item_id)
        if progression is None:
            progression = self._client.get_progression(item_id)
        locator = progression.get("locator") or {}
        href = locator.get("href")
        if not isinstance(href, str) or not href:
            return None
        locations = locator.get("locations") or {}
        title = locator.get("title")
        return ProviderBookmark(
            ref=href,
            title=title if isinstance(title, str) and title else None,
            progression=float(locations.get("progression") or 0.0),
            raw=progression,
        )

    def place_bookmark(
        self, item_id: str, anchor: ProviderAnchor, bookmark: ProviderBookmark
    ) -> bool:
        """Write a bookmark to a chapter anchor (``SPI 2.19`` ``ReadPositionAnchoring``).

        Implements the anchoring contract. Returns ``False`` when the anchor's ``ref``
        is not in the positions table or when Komga rejects the write.

        Args:
            item_id: The Komga book id.
            anchor: The target anchor (from ``list_anchors``).
            bookmark: The bookmark to place.

        Returns:
            ``True`` when Komga accepted the write, ``False`` otherwise.
        """
        chapter = [loc for loc in self._positions_for(item_id) if loc.get("href") == anchor.ref]
        if not chapter:
            return False
        chapter.sort(key=lambda loc: (loc.get("locations") or {}).get("position") or 0)
        want = bookmark.progression

        def distance_to_progression(loc: dict[str, Any]) -> float:
            """Absolute distance from a locator's progression to the requested value."""
            progression = float((loc.get("locations") or {}).get("progression") or 0.0)
            return abs(progression - want)

        chosen = min(chapter, key=distance_to_progression)
        # EXP-009: Komga's positions carry no titles, so a locator written without one makes the
        # *next* capture title-less. Write the title we are placing, so it round-trips.
        if bookmark.title:
            locator = {**chosen, "title": bookmark.title}
        else:
            locator = {k: v for k, v in chosen.items() if k != "title"}
        payload = {
            "modified": self._now().isoformat(),
            "device": {"id": "ebookerr", "name": "ebookerr"},
            "locator": locator,
        }
        logger.debug("Chosen Komga locator for restore: %s", locator)
        if not self._client.put_progression(item_id, payload):
            return False
        self._written_progression[item_id] = payload
        return True

    def _epub_path_for(self, book: BookView | None) -> Path | None:
        """The book's EPUB in the library, or ``None`` when either half is unknown.

        Used by :meth:`_document_hrefs` to build the anchor join's non-chapter-document list
        (``CHC-D12``); a ``None`` here is why that join falls back to fail-closed.

        Args:
            book: The merged book view, or ``None``.

        Returns:
            The path, or ``None`` when there is no library folder or no output filename.
        """
        if book is None or not self._library_folder or not book.output_filename:
            return None
        return self._library_folder / book.output_filename

    def _semantic_position(
        self,
        komga_book_id: str,
        komga_book: dict[str, Any],
        book: BookView | None,
        join: AnchorJoin,
    ) -> tuple[ReadPosition | None, dict[str, Any]]:
        """Capture semantic read position from Komga progression and positions.

        Returns the semantic position and the raw progression dict, with device/modified
        info preserved for backward-move warning and attribution. Refreshes
        ``_progression_cache`` with this fetch: on a first link, the pre-discovery snapshot
        cached at the top of ``_sync_reachable`` is an unconditional ``{}`` (there is no id to
        query yet), and leaving it in place would make :meth:`read_bookmark`'s cache lookup
        see a permanently-empty bookmark instead of Komga's real one.

        ``capture_position``'s finished check reads ``book.progress.completed``, but ``book``
        is the pre-sync merged view — on the exact sync where Komga first reports a book
        complete, that flag is still stale. Komga's progression payload carries no completed
        flag of its own for :meth:`read_bookmark` to surface instead (unlike a provider that
        marks it on the bookmark itself), so this passes a copy of ``book`` with ``completed``
        patched from this call's own freshly-read ``komga_book`` when it disagrees, mirroring
        :meth:`_read_back`'s own reading of ``readProgress.completed``.

        Args:
            komga_book_id: The Komga book id.
            komga_book: The current Komga book metadata dict.
            book: The merged book view, or ``None``.
            join: The anchor join, already computed from the provider's anchors and the
                book's chapter table (``CHC-D12``); ``capture_position`` fails closed on its
                own when this join is inconsistent.

        Returns:
            A tuple of (ReadPosition or None, raw progression dict).
        """
        progression = self._client.get_progression(komga_book_id)
        self._progression_cache[komga_book_id] = progression
        capture_book = book
        if book is not None and not book.progress.completed:
            freshly_finished = bool((komga_book.get("readProgress") or {}).get("completed"))
            if freshly_finished:
                capture_book = replace(book, progress=replace(book.progress, completed=True))
        result = (
            capture_position(
                self,
                komga_book_id,
                book=capture_book,
                join=join,
                stored=capture_book.read_position,
                captured_at=self._now().isoformat(),
                provider_name="Komga",
                book_title=capture_book.title,
            )
            if capture_book is not None
            else None
        )
        if result is not None:
            device = progression.get("device") or {}
            logger.debug(
                'Komga progression for "%s": href=%s, progression=%.3f, device=%s/%s, modified=%s',
                komga_book.get("title"),
                (progression.get("locator") or {}).get("href"),
                result.chapter_progress,
                device.get("id"),
                device.get("name"),
                progression.get("modified"),
            )
        return result, progression

    def _first_link_backup(
        self, book: BookView, komga_book_id: str, komga_book: dict[str, Any]
    ) -> ReadPosition | None:
        """DB-sourced RP-history backup on link/re-link to a progress-less provider (EXP-002).

        Fires only when this sync links the book to a Komga id it was not linked to
        before AND Komga reports no reading progress for it AND local state carries
        progress. A provider *with* progress still wins exactly as before.
        """
        if book.external.item_id == komga_book_id:
            return None  # ordinary re-sync of an existing link
        progress = komga_book.get("readProgress") or {}
        if progress.get("page") or progress.get("completed"):
            return None  # provider has progress — provider wins
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

    def _snapshot_with_db_fallback(
        self,
        book: BookView,
        komga_book_id: str,
        snapshot: dict[str, Any],
        *,
        relinked: bool = False,
    ) -> dict[str, Any]:
        """DB-fallback restore for the sync-start snapshot.

        Reuses the persisted locator (``book.progress.locator``) when the live
        snapshot is empty and the book is still linked to the same Komga book.
        A locator older than the newest read-position record is not replayed
        **unless that record stands at the locator's own chapter** (it was captured
        from this locator), so a restore is never undone by the capture it produced (EXP-123).

        ``relinked`` marks a sync where `_ensure_present` re-resolved a
        vanished Komga id for the same file, in which case the id mismatch is
        expected and the persisted locator still applies. Only a re-link that resolved
        the *same file* under a new id sets it; a re-link that corrected a wrong stored
        id does not, because the persisted locator then describes a different book.
        """
        if _has_progress(snapshot) or not book.progress.locator:
            return snapshot
        # A re-link points at the same physical book under a new Komga id, so the persisted
        # locator is still the reader's position; any other id mismatch is a different book.
        if book.external.item_id != komga_book_id and not relinked:
            return snapshot
        try:
            parsed: dict[str, Any] = json.loads(book.progress.locator)
        except (json.JSONDecodeError, TypeError):
            logger.warning('Corrupt persisted locator for "%s"; skipping restore', book.title)
            return snapshot
        # Guard (EXP-123): never replay a locator that predates a *different* position the app has
        # since recorded. A record captured from this very locator is not newer progress — the
        # restore that wrote it and the capture that read it back are milliseconds apart — so a
        # same-chapter record never vetoes the replay. Timestamps are compared as aware datetimes:
        # Komga stamps `modified` in its own offset, the app stamps `captured_at` in UTC.
        newest = book.read_position
        stamp = parse_datetime(parsed.get("modified"))
        newest_at = parse_datetime(newest.captured_at) if newest is not None else None
        locator_href = _href_basename((parsed.get("locator") or {}).get("href"))
        newest_href = _href_basename(newest.chapter_href) if newest is not None else None
        if (
            newest is not None
            and stamp is not None
            and newest_at is not None
            and stamp < newest_at
            and locator_href != newest_href
        ):
            logger.warning(
                'Persisted locator for "%s" is older than the newest read-position record and at a '
                "different chapter (locator modified=%s href=%s, newest captured_at=%s href=%s); "
                "not replaying it",
                book.title,
                parsed.get("modified"),
                locator_href,
                newest.captured_at,
                newest_href,
            )
            return snapshot
        if newest is not None and stamp is not None and newest_at is not None and stamp < newest_at:
            logger.debug(
                'Persisted locator for "%s" predates the newest record but stands at its chapter '
                "(href=%s); replaying it",
                book.title,
                locator_href,
            )
        return parsed

    def _repair_page_reset(
        self, komga_book_id: str, komga_book: dict[str, Any], book: BookView
    ) -> dict[str, Any]:
        """Re-write a surviving locator whose page Komga reset to 1 (``RP-D17``).

        Komga keeps the R2 locator across a re-analysis but resets ``readProgress.page`` to 1;
        re-PUTting the same locator with a fresh ``modified`` makes it recompute the page. Never
        moves the reader (same locator); returns the refreshed book record, or *komga_book* when
        nothing was written.

        Args:
            komga_book_id: The Komga book id to repair.
            komga_book: The book's current Komga record (``GET`` response body).
            book: The merged book view (for logging title).

        Returns:
            The refreshed book record after a successful repair, or *komga_book* unchanged.
        """
        media = komga_book.get("media") or {}
        read_progress = komga_book.get("readProgress") or {}

        pages = int(media.get("pagesCount") or 0)
        if pages <= 1:
            return komga_book

        page = int(read_progress.get("page") or 0)
        if page > 1:
            return komga_book

        if read_progress.get("completed"):
            return komga_book

        # Reuse cached progression from the re-anchor branch if available
        current = self._progression_cache.get(komga_book_id)
        if current is None:
            current = self._client.get_progression(komga_book_id)
        if not _has_progress(current):
            return komga_book

        locations = _locations(current) or {}
        total_progression = float(locations.get("totalProgression") or 0.0)
        if total_progression * pages <= 1.5:
            return komga_book

        # All conditions met: re-write the locator with a fresh modified timestamp
        payload = {**current, "modified": self._now().isoformat()}
        if not self._client.put_progression(komga_book_id, payload):
            logger.debug(
                'Komga refused the progression re-write for "%s" (item_id=%s); '
                "leaving its page as reported",
                book.title,
                komga_book_id,
            )
            return komga_book

        self._progression_cache[komga_book_id] = payload
        after = self._client.get_book(komga_book_id) or komga_book
        after_page = int((after.get("readProgress") or {}).get("page") or 0)
        logger.info(
            'Re-wrote the Komga progression for "%s" (item_id=%s) so its page is recomputed: '
            "page %d -> %d of %d",
            book.title,
            komga_book_id,
            page,
            after_page,
            pages,
        )
        return after

    def _restore_progress_if_lost(
        self, komga_book_id: str, snapshot: dict[str, Any], book: BookView | None = None
    ) -> dict[str, Any]:
        """Raw locator fast path (``RP-PLUG-4``): restore the pre-sync snapshot if lost.

        Protects **this sync's own** in-flight analyse/scan reset using the exact
        pre-sync ``R2Progression`` snapshot — runs on every sync where no semantic restore
        is pending **and** the core's anchor check reported the provider's bookmark still
        resolves (or had nothing to decide) (:meth:`_restore_semantic` is the separate,
        durable, cross-pull layer built on top of this one; the two never run in the
        same sync, see :meth:`sync`). No-op when the snapshot itself carried no
        meaningful progress.

        Args:
            komga_book_id: The Komga book id to check/restore progression for.
            snapshot: The progression captured at sync-start, before the EPUB/analyse
                could reset it.
            book: The merged book view (for logging title), or None.

        Returns:
            ``{"external_locator": ...}`` (the progression to persist, JSON-encoded)
            on either outcome that has one to record; ``{}`` when there was nothing to
            protect or the restore write failed.
        """
        if not _has_progress(snapshot):
            return {}  # nothing meaningful to protect
        current = self._client.get_progression(komga_book_id)
        if _has_progress(current):
            # Komga retained progress (possibly recomputed for the re-analyzed file);
            # reading state is Komga-mastered, so the snapshot defers to it.
            logger.debug(
                "Komga kept read progression for book %s; the pre-sync snapshot defers to it",
                komga_book_id,
            )
            return {"external_locator": json.dumps(current)}
        # Komga rejects progressions that are not strictly newer than its record,
        # so the snapshot is written back under a fresh `modified` timestamp.
        restored = {**snapshot, "modified": self._now().isoformat()}
        if book is not None and book.read_position is not None:
            logger.warning(
                'Replaying a pre-sync read progression for "%s" over the provider\'s empty state '
                "(newest app record: chapter_index=%d at %s) — verify it in your reader",
                book.title,
                book.read_position.chapter_index,
                book.read_position.captured_at,
            )
        if self._client.put_progression(komga_book_id, restored):
            title = book.title if book is not None else None
            logger.info(
                'Restored read progression for "%s" (komga_book_id=%s)',
                title or "(unknown title)",
                komga_book_id,
            )
            return {"external_locator": json.dumps(snapshot)}
        logger.warning(
            "Komga read-progression restore failed for book %s; "
            "the pre-sync snapshot could not be written back",
            komga_book_id,
        )
        return {}
