"""EpubChapterReorderPlugin — a staged first-party EpubPlugin, the reference dogfood plugin.

Wraps the shared ``epub_chapter_reorder.reorder_step.reorder_epub`` routine as an
``EpubPlugin`` (``FR-MIG-5``) so chapter reorder can run standalone through the plugin
dispatcher, independently of the pull pipeline. ``reorder_epub`` reads the EPUB's NCX
navMap, derives chapter numbers via the chapter-number heuristic
(:func:`~ebookerr_sdk.domain.chapter_number.extract_chapter_info`), and rewrites the NCX navMap
and OPF spine only when the order is actually wrong — an already-ordered EPUB is a no-op.

``EpubChapterReorderPlugin`` is the **only live reorder path**: a pull's finalize pass runs
every enabled EPUB plugin (this one included, ``priority=100``) against the staged file, so
pull-time reordering happens through this same plugin rather than a separate step. The older
``PostProcessRegistry``/``ChapterReorderStep`` prototype that ``reorder_epub`` was originally
written to share has since been removed along with the rest of the legacy download pipeline —
``reorder_epub`` now has this plugin as its sole caller.

Since ``2.0.0`` it is also **headed**: the ``Chapters`` action (``CHX-D1``) opens a
core-rendered chapter editor for one book at a time, while the headless path keeps
running unattended on ``EpubCreated``/``EpubModified``. A book carrying a stored manual
order (``CHX-D4``) is re-ordered to that order on every headless pass rather than back to
the automatic one; a headless run never clears the memory (``CHX-D5``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from ebookerr_sdk.domain.text_encoding import unicode_identity
from ebookerr_sdk.epub import EpubDocument, EpubError
from ebookerr_sdk.epub.chapters import (
    ChapterEntry,
    ChapterRole,
    chapter_key,
    chapter_keys,
    chapter_role,
    classify_spine,
)
from ebookerr_sdk.epub.errors import ItemNotFoundError
from ebookerr_sdk.epub.titles import resolve_book_title
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    ChapterLink,
    CustomValueDecl,
    CustomValueWrite,
    EpubItem,
    InvocationMode,
    PluginContext,
    PluginEventType,
    PluginManifest,
    PluginType,
    PluginView,
    SettingsSchema,
    UiTrigger,
    ViewItem,
    ViewSection,
    ViewSectionKind,
)

from epub_chapter_reorder.book_display import display_title
from epub_chapter_reorder.reorder_step import automatic_key_order, reorder_epub

logger = logging.getLogger(__name__)

_MANUAL_ORDER_KEY = "manual_order"
_MANUAL_ORDER_AT_KEY = "manual_order_at"

_FIXED_REASON_BY_ROLE: dict[ChapterRole, str] = {
    ChapterRole.TITLE: "The title page always comes first",
    ChapterRole.FRONT: "Front matter always stays before the chapters",
    ChapterRole.BACK: "Back matter always stays after the chapters",
}


def _zone_lock(entry: ChapterEntry) -> tuple[bool, str]:
    """Return ``(fixed, reason)`` for a row, judged exactly as the headless pass judges zones.

    ``reorder_epub`` bands rows by :func:`chapter_role` over title and number alone
    (``CHC-D5``); the editor must lock the same rows or it offers moves the next pass
    discards (``EXP-185``). ``ChapterEntry.role`` is *not* used here: the structural layer may
    mark a title page ``OTHER`` while the zone rule still keeps it first.

    Args:
        entry: The classified spine entry.

    Returns:
        ``(True, <reason>)`` for a title-page, front-matter or back-matter row; ``(False, "")``
        for a content row.
    """
    role = chapter_role(entry.title, entry.number)
    reason = _FIXED_REASON_BY_ROLE.get(role)
    return (True, reason) if reason is not None else (False, "")


def _stored_manual_order(book: BookView) -> list[str]:
    """Return the stored manual chapter-key order, or [] (CHX-TR-1).

    Reads ``book.custom_values["manual_order"]`` — a plugin receives only its own custom
    values, bare-keyed (the wire strips the ``epub_chapter_reorder.`` namespace) —
    and JSON-decodes it. A missing, malformed or non-list value yields ``[]``; this never
    raises. A degenerate order (duplicate keys) is discarded at read time.

    Args:
        book: The BookView to read from.

    Returns:
        A list of stable chapter keys, or [] if missing/malformed/degenerate.
    """
    key = _MANUAL_ORDER_KEY
    if key not in book.custom_values:
        return []

    view = book.custom_values[key]
    if not view.value:
        return []

    try:
        parsed = json.loads(view.value)
        if not isinstance(parsed, list):
            return []
        if len(set(parsed)) != len(parsed):
            logger.warning(
                "Stored manual chapter order for book_id=%s has %d key(s) but only %d distinct; "
                "discarding it (it encodes no ordering)",
                book.book_id,
                len(parsed),
                len(set(parsed)),
            )
            return []
        return parsed
    except json.JSONDecodeError:
        logger.warning(
            "Stored manual chapter order for book_id=%s is unreadable; ignoring it",
            book.book_id,
        )
        return []


def _duplicate_idrefs(
    doc: EpubDocument, entries: Sequence[ChapterEntry], *, story_url: str | None = None
) -> set[str]:
    """Return idrefs sharing identity with another entry (``CHX-D8``, ``EXP-126``, ``TXE-TR-1``).

    Two entries are flagged when they share a non-empty URL identity **other than the
    book's own ``story_url``**, or when their ``unicode_identity(title)`` values match and
    are non-empty. The book-level URL is excluded because ``epub_chapter_url`` deliberately
    stamps it on every chapter of a book that declares none, which would otherwise flag
    every chapter. Advisory only: the caller never pre-deselects or removes anything.

    Args:
        doc: The EPUB document.
        entries: The spine entries to check for duplicates.
        story_url: The book's story URL, if any. URLs matching this after stripping are
            excluded from identity checking.

    Returns:
        A set of idrefs that are flagged as duplicates (members of any group of size ≥ 2).
    """
    # Normalize story_url for comparison
    normalized_story_url = (story_url or "").strip() if story_url else ""

    # Track identity groups
    url_groups: dict[str, list[str]] = {}
    slug_groups: dict[str, list[str]] = {}

    for entry in entries:
        # Identity A: chapter_key when it starts with http:// or https://
        # Exclude the book's own story_url to avoid false positives
        key = chapter_key(doc, entry)
        if key.startswith(("http://", "https://")) and key != normalized_story_url:
            if key not in url_groups:
                url_groups[key] = []
            url_groups[key].append(entry.idref)

        # Identity B: unicode_identity(title) when non-empty
        title_slug = unicode_identity(entry.title)
        if title_slug:
            if title_slug not in slug_groups:
                slug_groups[title_slug] = []
            slug_groups[title_slug].append(entry.idref)

    # Collect idrefs from any group of size ≥ 2
    duplicates: set[str] = set()
    for group in url_groups.values():
        if len(group) >= 2:
            duplicates.update(group)
    for group in slug_groups.values():
        if len(group) >= 2:
            duplicates.update(group)

    return duplicates


def _build_view(
    doc: EpubDocument,
    book_title: str,
    entries: Sequence[ChapterEntry],
    *,
    has_manual_order: bool,
    story_url: str | None = None,
) -> PluginView:
    """Build the chapter editor's declarative view for one book (``CHX-D3``).

    Title-page, front-matter and back-matter rows are emitted ``fixed`` with the zone rule as
    their reason (``PV-D12``, ``EXP-185``).

    Args:
        doc: The EPUB document.
        book_title: The book's title (may be empty).
        entries: The classified spine entries.
        has_manual_order: Whether the book has a stored manual chapter order.
        story_url: The book's story URL, if any.

    Returns:
        A PluginView for the chapter editor.
    """
    # Detect duplicates once
    duplicate_idrefs = _duplicate_idrefs(doc, entries, story_url=story_url)

    # Build view items from entries
    items: list[ViewItem] = []
    for entry in entries:
        # id and sublabel are straightforward
        item_id = entry.idref
        sublabel = entry.href

        # label falls back from title to href
        label = entry.title or entry.href

        # Build badges in order: role, number, duplicate
        badges: list[str] = []

        # 1. Role badge (omit CONTENT)
        if entry.role != ChapterRole.CONTENT:
            role_badge = {
                ChapterRole.TITLE: "Title",
                ChapterRole.FRONT: "Front",
                ChapterRole.BACK: "Back",
                ChapterRole.OTHER: "Other",
            }.get(entry.role)
            if role_badge:
                badges.append(role_badge)

        # 2. Number badge
        if entry.number:
            badges.append(f"Ch. {entry.number}")

        # 3. Duplicate badge
        if item_id in duplicate_idrefs:
            badges.append("Duplicate")

        # Determine if row is fixed by zone rule
        fixed, fixed_reason = _zone_lock(entry)

        items.append(
            ViewItem(
                id=item_id,
                label=label,
                sublabel=sublabel,
                badges=tuple(badges),
                selected=True,
                locked=False,
                fixed=fixed,
                fixed_reason=fixed_reason,
            )
        )

    # Build sections
    sections: list[ViewSection] = []

    # Optional manual-order note
    if has_manual_order:
        sections.append(
            ViewSection(
                name="manual_order_note",
                kind=ViewSectionKind.NOTE,
                text=(
                    "This book has a manual chapter order. "
                    "Applying the automatic order will discard it."
                ),
            )
        )

    # Main chapters section
    sections.append(
        ViewSection(
            name="chapters",
            kind=ViewSectionKind.ITEM_LIST,
            label="Chapters",
            selectable=True,
            reorderable=True,
            select_all=True,
            min_selected=1,
            items=tuple(items),
        )
    )

    # Build view
    title = f"Chapters — {book_title}" if book_title else "Chapters"
    view = PluginView(
        title=title,
        description=(
            "Deselect a chapter to remove it from the EPUB. Drag to reorder. Title page, front "
            "matter and back matter keep their place. There is no undo."
        ),
        sections=tuple(sections),
        submit_label="Apply chapter changes",
        cancel_label="Cancel",
        danger=True,
    )

    return view


_MANIFEST = PluginManifest(
    id="epub_chapter_reorder",
    name="Chapter Reorder",
    description=(
        "Puts an EPUB's chapters back into numbered order when the source delivered them "
        "shuffled. A file already in order is left untouched."
    ),
    version="2.1.0",
    plugin_type=PluginType.EPUB,
    settings_schema=SettingsSchema(),
    headless=True,
    headed=True,
    priority=100,
    events=(PluginEventType.EPUB_CREATED, PluginEventType.EPUB_MODIFIED),
    ui_triggers=(
        UiTrigger(
            scope="book_selection_action",
            icon="low_priority",
            label="Chapters",
            description="Reorder chapters or set a manual order for this book.",
            min_books=1,
        ),
    ),
    custom_values=(
        CustomValueDecl(
            key="manual_order_at",
            type="datetime",
            label="Manual chapter order set",
        ),
    ),
    default_enabled=True,
    run_timeout_s=600,
    icon="reorder",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_chapter_reorder",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_chapter_reorder",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
)


class EpubChapterReorderPlugin:
    """Reorder out-of-order EPUB chapters (NCX navMap + OPF spine)."""

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the (empty) settings schema — this plugin has no user-configurable options."""
        return SettingsSchema()

    def _process_headed(self, item: EpubItem, ctx: PluginContext) -> BookPatch | None:
        """Raise the chapter editor for one book and apply what the user chose (``CHX-D6``).

        Args:
            item: The staged EPUB item to edit.
            ctx: Plugin context with request_view and logging.

        Returns:
            A BookPatch with the edited file's recomputed chapter links, or None if cancelled.
        """
        # Open the document and classify
        doc = EpubDocument.open(item.epub_path)
        entries = classify_spine(doc)
        book_title = display_title(item.book.title, epub_title=resolve_book_title(doc))

        # Capture the original spine order before any modifications
        original_spine = [e.idref for e in entries]

        # Check for stored manual order
        stored = _stored_manual_order(item.book)

        # Build the view
        dupes = _duplicate_idrefs(doc, entries, story_url=item.book.story_url)
        view = _build_view(
            doc, book_title, entries, has_manual_order=bool(stored), story_url=item.book.story_url
        )

        # Count fixed rows in the chapters section (last section)
        fixed_count = sum(1 for item in view.sections[-1].items if item.fixed)

        ctx.logger.info(
            'Chapter editor opened for "%s" (book_id=%s): %d chapter(s), '
            "%d fixed by zone, %d duplicate hint(s)",
            book_title,
            item.book.book_id,
            len(entries),
            fixed_count,
            len(dupes),
        )

        # Request the view
        result = ctx.request_view(view)

        # Check if cancelled
        if not result.submitted:
            ctx.logger.info(
                'Chapter editor cancelled for "%s" (book_id=%s)',
                book_title,
                item.book.book_id,
            )
            return None

        # Get the selection
        selection = result.selections.get("chapters")
        if selection is None:
            ctx.logger.info(
                'Chapter editor cancelled for "%s" (book_id=%s)',
                book_title,
                item.book.book_id,
            )
            return None

        # Derive kept and removed idrefs
        keep = [idref for idref in selection.order if idref in set(selection.selected)]
        removed = [e.idref for e in entries if e.idref not in set(selection.selected)]

        # Detect whether anything actually changed
        order_changed = keep != [i for i in original_spine if i in set(keep)]
        anything_changed = bool(removed) or order_changed

        # If nothing changed on disk, it's a true no-op only when nothing is stored either
        # (EXP-128). A stored manual order that the user just re-confirmed as the automatic
        # order still needs clearing — that write needs no file rewrite, so it is returned
        # directly rather than falling through to the remove/reorder/save steps below.
        if not anything_changed:
            if not stored:
                ctx.logger.info(
                    'Chapter editor applied no change to "%s" (book_id=%s): %d chapter(s) kept, '
                    "the EPUB was not rewritten and no manual order was stored",
                    book_title,
                    item.book.book_id,
                    len(keep),
                )
                return None

            now_iso = datetime.now(UTC).isoformat()
            ctx.logger.info(
                'Manual chapter order cleared for "%s" (book_id=%s) by an explicit automatic '
                "reorder",
                book_title,
                item.book.book_id,
            )
            return BookPatch(
                book_id=item.book.book_id,
                custom_values={
                    _MANUAL_ORDER_KEY: CustomValueWrite(
                        value="", value_type="string", updated_at=now_iso, delete=True
                    ),
                    _MANUAL_ORDER_AT_KEY: CustomValueWrite(
                        value="", value_type="datetime", updated_at=now_iso, delete=True
                    ),
                },
            )

        # Remove chapters
        for idref in removed:
            try:
                doc.remove_chapter(idref)
            except ItemNotFoundError:
                ctx.logger.warning(
                    'Chapter %s not found while removing from "%s"; skipping',
                    idref,
                    book_title,
                )

        # Build nav_by_idref mapping
        nav_by_idref: dict[str, str] = {}
        for point in doc.ncx.nav_points():
            item_by_href = doc.opf.item_by_href(point.src)
            if item_by_href is not None and item_by_href.id not in nav_by_idref:
                nav_by_idref[item_by_href.id] = point.id

        # Reorder
        nav_ids = [nav_by_idref[i] for i in keep if i in nav_by_idref]
        doc.ncx.reorder(nav_ids)

        spine_idrefs = [i for i in keep if doc.opf.item_by_id(i) is not None]
        doc.opf.reorder_spine(spine_idrefs)

        # Save
        doc.save()

        # Count moved positions
        original_kept = [idref for idref in original_spine if idref in set(keep)]
        moved = sum(
            1
            for i, idref in enumerate(keep)
            if i < len(original_kept) and idref != original_kept[i]
        )

        ctx.logger.info(
            'Chapter editor applied to "%s" (book_id=%s): %d removed, %d reordered, %d kept',
            book_title,
            item.book.book_id,
            len(removed),
            moved,
            len(keep),
        )

        # Recompute chapter keys from the edited document
        doc_after = EpubDocument.open(item.epub_path)
        entries_after = classify_spine(doc_after)
        key_map = chapter_keys(doc_after, entries_after)
        keys = [key_map[e.idref] for e in entries_after if key_map[e.idref]]

        # Compute the automatic order for comparison
        automatic_order = automatic_key_order(doc_after)

        # Decide whether to store or clear the order
        now_iso = datetime.now(UTC).isoformat()
        custom_values: dict[str, CustomValueWrite] = {}

        if stored and keys == automatic_order:
            # Clear the stored order since user submitted the automatic order
            custom_values[_MANUAL_ORDER_KEY] = CustomValueWrite(
                value="", value_type="string", updated_at=now_iso, delete=True
            )
            custom_values[_MANUAL_ORDER_AT_KEY] = CustomValueWrite(
                value="", value_type="datetime", updated_at=now_iso, delete=True
            )
            ctx.logger.info(
                'Manual chapter order cleared for "%s" (book_id=%s) '
                "by an explicit automatic reorder",
                book_title,
                item.book.book_id,
            )
        else:
            # Store the new order
            custom_values[_MANUAL_ORDER_KEY] = CustomValueWrite(
                value=json.dumps(keys), value_type="string", updated_at=now_iso
            )
            custom_values[_MANUAL_ORDER_AT_KEY] = CustomValueWrite(
                value=now_iso, value_type="datetime", updated_at=now_iso
            )
            ctx.logger.info(
                'Manual chapter order stored for "%s" (book_id=%s): %d key(s)',
                book_title,
                item.book.book_id,
                len(keys),
            )

        # Return patch with chapter links and custom values
        return BookPatch(
            book_id=item.book.book_id,
            chapters=tuple(
                ChapterLink(url=url, title=title, ordinal=ordinal)
                for url, title, ordinal in doc_after.chapter_links()
            ),
            custom_values=custom_values,
        )

    def process(self, items: tuple[EpubItem, ...], ctx: PluginContext) -> list[BookPatch]:
        """Reorder each staged EPUB's chapters and report progress across the batch.

        Args:
            items: Staged EPUB items to reorder in place.
            ctx: Plugin context; used for cancellation checks and progress reporting.

        Returns:
            One ``BookPatch(fields={"num_chapters": ...})`` per EPUB that was actually
            reordered; already-ordered EPUBs contribute nothing. A malformed EPUB is
            skipped (logged as a warning) rather than failing the whole batch.
        """
        patches: list[BookPatch] = []
        total = len(items)
        for i, item in enumerate(items):
            ctx.check_cancelled()
            if ctx.mode is InvocationMode.HEADED:
                try:
                    patch = self._process_headed(item, ctx)
                except EpubError:
                    logger.warning(
                        "Chapter editor skipped for %s (malformed EPUB)",
                        item.epub_path,
                        exc_info=True,
                    )
                    patch = None
                if patch is not None:
                    patches.append(patch)
                ctx.report((i + 1) / total * 100.0 if total else 100.0)
                continue

            # Headless path
            try:
                stored = _stored_manual_order(item.book)
                if stored:
                    logger.debug(
                        "Applying a stored manual chapter order to %s: %d key(s)",
                        item.epub_path,
                        len(stored),
                    )
                changed = reorder_epub(
                    item.epub_path,
                    manual_order=stored or None,
                    display_title=item.book.title,
                )
            except EpubError:
                logger.warning(
                    "Chapter-reorder skipped for %s (malformed EPUB)",
                    item.epub_path,
                    exc_info=True,
                )
                ctx.report((i + 1) / total * 100.0 if total else 100.0)
                continue
            if changed:
                num_chapters = EpubDocument.open(item.epub_path).content_chapter_count()
                patches.append(
                    BookPatch(
                        book_id=item.book.book_id,
                        fields={"num_chapters": num_chapters},
                    )
                )
            ctx.report((i + 1) / total * 100.0 if total else 100.0)
        return patches
