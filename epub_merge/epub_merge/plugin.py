"""EpubMergePlugin — the accepts_list reference EpubPlugin, retired rule-bending reworded.

Merges the content chapters of every selected book into the survivor (elected from the books
themselves, not the caller's ordering), then deletes the rest. Manual-only (``events=()``)
and headed-only (``headless=False``): headless EpubEditSession passes skip it entirely, so it
only ever runs from the
book-selection "Merge" action (``min_books=2``, guarded again at runtime even though the
UI already keeps ``min_books`` from letting a caller invoke it with fewer). ``process`` also
checks ``ctx.mode`` itself before requesting anything, so a headless caller that somehow still
reached it returns empty without ever raising a view.

The destructive step is guarded by a declarative confirmation view (``DEC-77``) with three
sections: a survivor picker (select control) letting the user confirm or override the election,
a neutral note, and an item-list where every selected book is offered (the survivor is locked
and unselected; others are pre-selected). The user may deselect any non-survivor before merging;
a cancelled view or an empty selection aborts the merge. This retires the ``ui_context``
pre-confirmation back-channel introduced by ``DEC-64`` — a magic string standing in for a
confirmation contract — so no caller can skip this plugin's own confirmation any more.

Declares two user-configurable settings: ``rewrite_title_page`` (regenerate the merged book's
title page from its metadata instead of keeping the first book's) and ``rewrite_book_title``
(rename the merged book to "<series> <first>-<last>" using chapter numbers found in the merged
chapters — the merge reports the title it wrote; the Book record is never renamed from what
the file happens to say (EXP-205)).

``merge_epubs`` validates its own result before anything is saved (see
``epub_merge.merge``). If it raises ``EpubMergeError`` — a source's
chapters weren't fully accounted for — ``process`` alerts the user with the
reason and returns no patches, so the survivor's EPUB is left untouched and
none of the other selected books are deleted: a failed merge can never look
like a successful one that silently dropped content.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.epub.chapters import Chapter, chapter_table
from ebookerr_sdk.spi import (
    BookPatch,
    ChapterLink,
    EpubItem,
    InvocationMode,
    PluginContext,
    PluginManifest,
    PluginType,
    PluginView,
    ReadPosition,
    SettingsField,
    SettingsSchema,
    UiTrigger,
    ViewItem,
    ViewSection,
    ViewSectionKind,
)

from epub_merge.merge import (
    EpubMergeError,
    MergeContentError,
    MergeInputError,
    MergeOptions,
    MergeStructureError,
    candidate_from_epub,
    elect_survivor,
    merge_epubs,
)
from epub_merge.merge.read_positions import (
    SourcePosition,
    merge_read_positions,
)

logger = logging.getLogger(__name__)

_SETTINGS_SCHEMA = SettingsSchema(
    fields=(
        SettingsField(
            key="rewrite_title_page",
            type="bool",
            label="Rewrite the title page",
            default=False,
            help="Regenerate the merged book's title page from its own metadata "
            "(title, authors, categories, language) instead of keeping the first book's.",
        ),
        SettingsField(
            key="rewrite_book_title",
            type="bool",
            label="Rewrite the book title with the chapter range",
            default=False,
            help='Rename the merged book to "<series> <first>-<last>" using the chapter '
            "numbers found in the merged chapters, e.g. Three Square Meals 174-180.",
        ),
    ),
    summary=(
        "Rewrite title page: {rewrite_title_page} · Chapter range in title: {rewrite_book_title}"
    ),
)

_MANIFEST = PluginManifest(
    id="epub_merge",
    name="EPUB Merge",
    description=(
        "Combines several books into one, moving every chapter into the first book and "
        "deleting the others. Run it from the Merge action; there is no undo."
    ),
    version="2.1.0",
    plugin_type=PluginType.EPUB,
    settings_schema=_SETTINGS_SCHEMA,
    headless=False,
    headed=True,
    priority=50,
    accepts_list=True,
    events=(),
    ui_triggers=(
        UiTrigger(
            scope="book_selection_action",
            icon="merge",
            label="Merge",
            description="Combine multiple selected books into one.",
            min_books=2,
        ),
    ),
    handles_merge_proposals=True,
    default_enabled=True,
    run_timeout_s=900,
    icon="merge",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_merge",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_merge",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
)


def _build_view(
    survivor: EpubItem, items: Sequence[EpubItem], elected_survivor_id: str
) -> PluginView:
    """Build the merge confirmation view: survivor picker, neutral note, and full item list.

    Args:
        survivor: The book that absorbs every selected book's chapters (may be overridden).
        items: Every selected book, including the survivor.
        elected_survivor_id: The book_id of the initially elected survivor (for comparison).

    Returns:
        A ``PluginView`` titled "Merge chapters" with three sections in order:
        1. FIELDS section with a select control to confirm or change the survivor.
           The picker's options are book_ids (options_strict=True).
        2. NOTE section with survivor-neutral text (``DEC-77``).
        3. ITEM_LIST section (name ``"orphans"``, heading **Books to merge**) of every
           book, with the survivor's row locked and unselected; all others pre-selected.
           Each checkbox is named ``"Merge <title>"`` — what its checked state does.
           The submit label names how many non-survivors the merge destroys
           (``EXP-049``/``EXP-101``): e.g. "Merge 3 books" or "Merge 1 book".
    """
    # Survivor picker section
    survivor_section = ViewSection(
        name="survivor",
        kind=ViewSectionKind.FIELDS,
        fields=(
            SettingsField(
                key="survivor",
                type="select",
                label="Keep this book",
                default=elected_survivor_id,
                options=tuple(item.book.book_id for item in items),
                option_labels=tuple(item.book.title or item.book.book_id for item in items),
                options_strict=True,
                help="Its chapters come first and it keeps its identity — its link to your library "
                "server, its reading positions, its chapter history and its cover. Every other "
                "book below is merged into it and deleted.",
            ),
        ),
    )

    # Neutral note text (doesn't mention the survivor by name)
    note_text = (
        "Every book you leave ticked is merged into the book above and then deleted. "
        "This cannot be undone."
    )
    note_section = ViewSection(name="note", kind=ViewSectionKind.NOTE, text=note_text)

    # Item list: all selected books, with survivor locked. `locked` (SPI 2.4) means "a
    # locked, SELECTED item can never be deselected" — the survivor's row is therefore
    # `selected=True` like every other row, not False: a locked-but-unselected row fails
    # the server's own validation (plugin_view_wire.py's "locked item may not be
    # deselected"), which only ever accepts a locked item that stays selected. The row's
    # checkbox still renders disabled (locked=True), so the user cannot uncheck it either
    # way; the merge itself always excludes the survivor from `others` by id, never by
    # reading its selected state, so this carries no behavior change downstream.
    list_items = tuple(
        ViewItem(
            id=item.book.book_id,
            label=item.book.title or item.book.book_id,
            sublabel=item.book.author or "",
            selected=True,
            locked=item.book.book_id == elected_survivor_id,
        )
        for item in items
    )
    orphans_section = ViewSection(
        name="orphans",
        kind=ViewSectionKind.ITEM_LIST,
        label="Books to merge",
        selectable=True,
        items=list_items,
        item_verb="Merge",
    )

    # Count non-survivors for the submit label
    non_survivors = sum(1 for item in items if item.book.book_id != elected_survivor_id)
    noun = "book" if non_survivors == 1 else "books"

    return PluginView(
        title="Merge chapters",
        sections=(survivor_section, note_section, orphans_section),
        submit_label=f"Merge {non_survivors} {noun}",
        danger=True,
    )


def _absorbed_chapter_links(
    survivor: EpubItem, others: Sequence[EpubItem], merged: Sequence[Chapter]
) -> tuple[ChapterLink, ...]:
    """Every story URL the merged book now contains, in a stable order.

    For each merged chapter whose key starts with http:// or https://, emits a ChapterLink
    with that key as URL and the chapter's ordinal. Then emits the survivor's own chapter URLs
    (its more specific identity) before its story URL, then each absorbed book's story URL
    before that book's own chapter URLs, all with ordinal 0. Deduplicated by URL keeping the
    first occurrence, so re-merging is idempotent.

    Without this the merged-away books' URLs vanish with their rows, and the Stories page
    stops recognising stories whose content is sitting inside the merged EPUB.

    Args:
        survivor: The book that absorbs every other item's chapters.
        others: The books being merged away and deleted.
        merged: The merged book's chapter table with ``ordinal`` and ``key`` attributes.

    Returns:
        The union as ``ChapterLink`` rows with proper ordinals (``CHC-D13``).
    """
    links: list[ChapterLink] = []
    seen: set[str] = set()

    def add(url: str | None, title: str | None, ordinal: int = 0) -> None:
        """Append one link when its URL is new and non-empty."""
        if not url or url in seen:
            return
        seen.add(url)
        links.append(ChapterLink(url=url, title=title, ordinal=ordinal))

    # First: merged chapters with URLs as their key
    for ch in merged:
        if ch.key and (ch.key.startswith("http://") or ch.key.startswith("https://")):
            add(ch.key, ch.title, ordinal=ch.ordinal)

    # Then: survivor's chapter URLs, then its story URL
    for link in survivor.book.chapters:
        add(link.url, link.title, ordinal=0)
    add(survivor.book.story_url, survivor.book.title, ordinal=0)

    # Finally: each absorbed book's story URL, then that book's own chapter URLs
    for item in others:
        add(item.book.story_url, item.book.title, ordinal=0)
        for link in item.book.chapters:
            add(link.url, link.title, ordinal=0)

    return tuple(links)


def _fail(
    ctx: PluginContext, survivor: EpubItem, exc: EpubMergeError, message: str
) -> list[BookPatch]:
    """Log and alert on merge failure; fail closed by returning no patches.

    Args:
        ctx: Plugin context for alerting the user.
        survivor: The target book that was not modified.
        exc: The exception raised by merge_epubs.
        message: The user-facing alert message to display.

    Returns:
        Empty list (nothing is deleted, the survivor is untouched).
    """
    logger.error(
        'EPUB merge into "%s" (book_id=%s) failed (%s): %s',
        survivor.book.title,
        survivor.book.book_id,
        type(exc).__name__,
        exc,
    )
    ctx.alert(message)
    return []


def _reject_unmergeable_selection(items: tuple[EpubItem, ...], ctx: PluginContext) -> bool:
    """Alert and refuse a selection that cannot be merged; leave a valid one untouched.

    Two independent guards: too few items to merge, and two or more items resolving to
    one library file (``EXP-249``) — merging those would concatenate a file with itself.
    The file-identity condition is ``output_filename``, not the staged ``epub_path``, since
    the runtime may stage one library file to two temporary copies, which a path comparison
    would miss.

    Args:
        items: The selected books.
        ctx: Plugin context, used to alert the user on refusal.

    Returns:
        True if the selection was refused (the user has already been alerted); False if it
        may proceed.
    """
    if len(items) < 2:
        ctx.alert("Select at least two books to merge.")
        return True

    files = [item.book.output_filename for item in items if item.book.output_filename]
    if len(files) != len(set(files)):
        logger.warning(
            "EPUB merge refused: %d selected book(s) resolve to %d distinct library file(s) "
            "— merging would copy a file into itself",
            len(items),
            len(set(files)),
        )
        ctx.alert(
            "Some of the selected books share a file on disk, so merging them would copy a "
            "book into itself. Select books that each have their own EPUB."
        )
        return True

    return False


def _announce_outcome(
    ctx: PluginContext,
    *,
    survivor_title: str,
    chapters_gained: int,
    books_deleted: int,
    books_remaining: int,
    failed: int,
) -> None:
    """Tell the user what this merge did, in the merge plugin's own vocabulary.

    The core does not know what a target, an orphan or an absorbed chapter is; the plugin
    that performs the operation is the one that can name them (``EXP-271``). A completed
    merge publishes one success toast; an incomplete one records the same diagnostic the
    core used to log, so the Logs page reads exactly as before.

    Args:
        ctx: The plugin invocation context (``notify``, ``logger``).
        survivor_title: The surviving book's title, already resolved.
        chapters_gained: How many chapters the survivor absorbed, never negative.
        books_deleted: How many merged-away books were removed.
        books_remaining: How many offered books are still present.
        failed: The batch's failed count, logged and not decided on.
    """
    if books_deleted:
        ctx.logger.info(
            'Merge absorbed %d chapter(s) into "%s"; %d book(s) deleted',
            chapters_gained,
            survivor_title,
            books_deleted,
        )
        ctx.notify(
            "success",
            f'Merged {chapters_gained} chapter(s) into "{survivor_title}"; '
            f"{books_deleted} book(s) deleted",
        )
    else:
        ctx.logger.info(
            "Merge did not complete: %d book(s) still present, %d failed",
            books_remaining,
            failed,
        )


class EpubMergePlugin:
    """Merge every selected book's chapters into the first, then delete the rest."""

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the settings schema with title page and book title rewrite options."""
        return _SETTINGS_SCHEMA

    def process(  # noqa: C901
        self, items: tuple[EpubItem, ...], ctx: PluginContext
    ) -> list[BookPatch]:
        """Merge every selected book's chapters into the elected survivor, then delete the rest.

        The survivor is **elected** from the books themselves by ``elect_survivor``, never
        taken from the caller's ordering — a merge must not depend on which book the user
        clicked first. The user may override the election via a select control in the
        confirmation view (the picker); an unknown choice is logged as a warning and ignored,
        keeping the elected survivor (fail closed).

        Reports staged progress through ``ctx.report`` at 5 / 15 / 80 / 100 on the success path.
        Failure paths (too few items, headless, a cancelled or empty confirmation, merge error)
        stop at whatever stage they reached and do not report 100.

        Args:
            items: The selected books; requires at least two items.
            ctx: Plugin context; used to confirm the destructive step through a declarative
                view (``DEC-77``) with a survivor picker and item-list selection, and to alert
                the user on a validation failure, too-small a selection, or a shared-file refusal
                (``EXP-249``). In ``HEADLESS`` mode the merge never runs and no view is
                requested; the elected survivor stands unchallenged and the retired
                ``DEC-64`` pre-confirmation ``ui_context`` back-channel is no longer read.

        Returns:
            On success: one ``BookPatch`` for the survivor (or the user's chosen override) with
            ``fields={"num_chapters": total}`` and, only when the ``rewrite_book_title``
            setting produced a rename, ``"title": <that title>``, zero or more
            ``BookPatch(read_position=...)`` entries (oldest first) carrying each input's
            remapped semantic read position onto the merged spine, then one
            ``BookPatch(delete=True, superseded_by=<survivor id>)`` per merged-away book
            (including the elected survivor if the user overrode it).
            ``superseded_by`` (SPI 2.23) tells the core which book absorbed each one.
            Empty list when the selection is too small, two or more selected books share
            an ``output_filename`` (``EXP-249``), the caller is headless, the user
            cancels the confirmation view or clears its selection, or the merge fails
            validation.

            On failure: empty list. The survivor's EPUB is not modified on disk, and
            nothing is deleted — fail closed.

            The core's post-publish hook dispatches ``EpubModified`` and — because a merge
            always changes the survivor's packaged chapter count — ``BookUpdated`` for the
            survivor, so the follow-up plugins run without this plugin requesting anything
            (EXP-202).

            The merge is given each input's own story URL, so a merged-away one-chapter
            book's address is stamped into the merged EPUB itself, not only into the
            database index.

        Raises:
            Whatever ``ctx.check_cancelled()`` raises at the three checkpoints (out of
            process the core cancels by stopping the plugin process).
        """
        logger.debug(
            "EPUB merge invoked for %d book(s); cancellation checkpoints armed", len(items)
        )
        ctx.report(5.0)

        ctx.check_cancelled()

        if _reject_unmergeable_selection(items, ctx):
            return []

        candidates = [
            candidate_from_epub(
                item.book.book_id,
                item.book.title or "",
                item.epub_path,
                getattr(item.book, "created_at", None),
            )
            for item in items
        ]
        survivor_id = elect_survivor(candidates)
        by_id = {item.book.book_id: item for item in items}
        survivor = by_id[survivor_id]
        others = [item for item in items if item.book.book_id != survivor_id]
        logger.info(
            'EPUB merge requested: %d book(s) into "%s" (book_id=%s)',
            len(items),
            survivor.book.title,
            survivor.book.book_id,
        )

        if ctx.mode is InvocationMode.HEADLESS:
            logger.info("Merge skipped: headless invocations never merge")
            return []

        view = _build_view(survivor, items, survivor_id)
        result = ctx.request_view(view)

        # Handle survivor override from the picker
        chosen = result.values.get("survivor", {}).get("survivor") if result.values else None
        if chosen and chosen != survivor_id and chosen in by_id:
            logger.info(
                'Merge survivor overridden by the user: elected "%s" (book_id=%s), '
                'chose "%s" (book_id=%s)',
                survivor.book.title,
                survivor.book.book_id,
                by_id[chosen].book.title,
                chosen,
            )
            survivor = by_id[chosen]
            survivor_id = chosen
            others = [item for item in items if item.book.book_id != chosen]
        elif chosen and chosen not in by_id:
            logger.warning(
                "Merge survivor override ignored: %r is not one of the selected books",
                chosen,
            )

        selection = result.selections.get("orphans")
        if not result.submitted or selection is None or not selection.selected:
            logger.info("Merge cancelled: no chapter selected")
            return []

        selected_ids = set(selection.selected)
        # Rebuild the mapping to include all current items (may have changed due to override)
        by_id_current = {item.book.book_id: item for item in items}
        # Filter selection to only include non-survivors from the current items
        others = [
            by_id_current[book_id]
            for book_id in selection.order
            if book_id in selected_ids and book_id != survivor_id
        ]

        ctx.report(15.0)

        ctx.check_cancelled()

        logger.debug(
            "EPUB merge stages: %d book(s) into book_id=%s",
            len(items),
            survivor.book.book_id,
        )

        options = MergeOptions(
            rewrite_title_page=bool(ctx.settings.get("rewrite_title_page", False)),
            rewrite_book_title=bool(ctx.settings.get("rewrite_book_title", False)),
        )

        try:
            outcome = merge_epubs(
                survivor.epub_path,
                [item.epub_path for item in others],
                options,
                book_urls=[survivor.book.story_url, *(item.book.story_url for item in others)],
            )
            total = outcome.chapter_count
        except MergeInputError as exc:
            return _fail(
                ctx,
                survivor,
                exc,
                f"Merge failed — one of the selected books could not be read: {exc}",
            )
        except MergeContentError as exc:
            return _fail(
                ctx,
                survivor,
                exc,
                f"Merge failed — the merged book did not account for every chapter: {exc}",
            )
        except MergeStructureError as exc:
            return _fail(
                ctx,
                survivor,
                exc,
                f'Merge failed — "{survivor.book.title}" has an internal structure problem, '
                "so the merged book could not be assembled. Retrying will not help. "
                "Run EPUB Validate on this book to see the details, then re-pull or "
                f"normalise it before merging again. Technical detail: {exc}",
            )
        except EpubMergeError as exc:
            return _fail(ctx, survivor, exc, f"Merge failed: {exc}")

        ctx.report(80.0)

        ctx.check_cancelled()

        # Read the merged EPUB to get chapter table (RP-D16)
        merged_doc = EpubDocument.open(survivor.epub_path)
        merged_chapters_tuple = chapter_table(merged_doc)

        # Create merged chapters list indexed by ordinal (subtract 1 when accessing)
        merged_chapters = list(merged_chapters_tuple)

        title = survivor.book.title or survivor.book.book_id
        merged_fields: dict[str, int | str] = {"num_chapters": outcome.chapter_count}
        if outcome.title is not None:
            merged_fields["title"] = outcome.title
            title = outcome.title
            logger.info(
                'EPUB merge renamed "%s" to "%s" (book_id=%s) as the rewrite-title setting asks',
                survivor.book.title,
                outcome.title,
                survivor.book.book_id,
            )

        absorbed = _absorbed_chapter_links(survivor, others, merged_chapters)
        logger.info(
            'EPUB merge absorbed %d chapter URL(s) into "%s" (book_id=%s)',
            len(absorbed),
            title,
            survivor.book.book_id,
        )

        # Read positions survive the merge (RP-D7). Every input's progress is remapped onto the
        # merged chapter numbering and the most advanced wins; the entries are emitted oldest
        # first so each one appends to the survivor's history in order, and the newest ends up
        # as the book's current position.
        all_items = [survivor, *others]
        source_positions = [
            SourcePosition(
                input_index=i,
                chapter_index=item.book.read_position.chapter_index,
                chapter_progress=item.book.read_position.chapter_progress,
                completed=item.book.read_position.completed,
                captured_at=item.book.read_position.captured_at,
                chapter_href=item.book.read_position.chapter_href,
                chapter_key=item.book.read_position.chapter_key,
            )
            for i, item in enumerate(all_items)
            if item.book.read_position is not None
        ]
        merged_positions = merge_read_positions(
            source_positions, outcome.contributions, outcome.chapter_map, merged_chapters
        )

        # Check for inferred positions and notify user (RP-D16)
        inferred_positions = [p for p in merged_positions if p.inferred]
        by_identity = len(inferred_positions) == 0

        if inferred_positions:
            # Notify about the first inferred position, using a generic title
            for item in all_items:
                if item.book.read_position is not None:
                    item_title = item.book.title or item.book.book_id
                    logger.warning(
                        "Merge placed the read position of %r (book_id=%s) by chapter "
                        "count; the reader should verify it",
                        item_title,
                        item.book.book_id,
                    )
                    ctx.notify(
                        "warning",
                        f"The reading position of {item_title!r} was placed by chapter "
                        "count, not by identity — check where your reader resumes.",
                        durable=True,
                    )
                    break

        if merged_positions:
            logger.info(
                'Merged read positions for "%s" (book_id=%s): %d source(s) -> chapter %d at %.0f%%'
                " (completed=%s), by identity=%s",
                title,
                survivor.book.book_id,
                len(source_positions),
                merged_positions[-1].chapter_index,
                merged_positions[-1].chapter_progress * 100,
                merged_positions[-1].completed,
                by_identity,
            )
        else:
            logger.info(
                'No read positions to merge for "%s" (book_id=%s): %d source book(s) had none',
                title,
                survivor.book.book_id,
                len(all_items),
            )

        logger.info(
            'EPUB merge applied: "%s" (book_id=%s) now has %d chapter(s); %d book(s) to delete',
            title,
            survivor.book.book_id,
            total,
            len(others),
        )

        ctx.report(100.0)

        # Alert on duplicate chapter numbers (R17)
        if outcome.duplicate_numbers:
            duplicate_str = ", ".join(outcome.duplicate_numbers)
            ctx.alert(
                f"Merged, but {len(outcome.duplicate_numbers)} chapter number(s) now appear more "
                f"than once: {duplicate_str}. The books you merged overlap — check the "
                "chapter list on the book's detail page."
            )
            logger.warning(
                'Merge overlap reported to the user for "%s" (book_id=%s): %s',
                title,
                survivor.book.book_id,
                ", ".join(outcome.duplicate_numbers),
            )

        chapters_gained = max((outcome.chapter_count or 0) - (survivor.book.num_chapters or 0), 0)
        _announce_outcome(
            ctx,
            survivor_title=title,
            chapters_gained=chapters_gained,
            books_deleted=len(others),
            books_remaining=0,
            failed=0,
        )

        return [
            BookPatch(
                book_id=survivor.book.book_id,
                fields=merged_fields,
                chapters=absorbed,
            ),
            *(
                BookPatch(
                    book_id=survivor.book.book_id,
                    read_position=ReadPosition(
                        chapter_index=p.chapter_index,
                        chapter_progress=p.chapter_progress,
                        completed=p.completed,
                        captured_at=p.captured_at,
                        total_chapters=outcome.chapter_count,
                        chapter_number=p.chapter_number,
                        chapter_title=p.chapter_title,
                        chapter_href=p.chapter_href,
                        chapter_key=p.chapter_key,
                    ),
                )
                for p in merged_positions
            ),
            *(
                BookPatch(
                    book_id=item.book.book_id,
                    delete=True,
                    superseded_by=survivor.book.book_id,
                )
                for item in others
            ),
        ]
