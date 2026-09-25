"""F9 — reorder out-of-order EPUB chapters.

FanFicFare sometimes lists a story's chapters out of chronological order (the real
"Tending Bar" lists ``Part 5, Part 6, Pt. 01..04``). ``reorder_epub`` reads the
EPUB's own table of contents — the book title and each chapter title — rather than
the FanFicFare JSON or the chapter ``<meta name="chapterurl">`` headers, since the
EPUB alone is self-sufficient.

Book titles for log lines are resolved by ``epub_chapter_reorder.book_display.display_title``,
preferring the caller's override-merged title, falling back to
:func:`~ebookerr_sdk.epub.titles.resolve_book_title` (NCX ``<docTitle>`` over OPF
``<dc:title>``); chapter titles by :func:`~ebookerr_sdk.epub.titles.resolve_nav_title`,
preferring ``<navLabel>`` over XHTML title. Each chapter's number is derived by the shared
:func:`~ebookerr_sdk.epub.chapter_numbers.nav_chapter_numbers` (F8's
:func:`~ebookerr_sdk.domain.chapter_number.extract_chapter_info` with the resolved book title as a
hint), the same derivation chapter adoption uses (``TR-AMG-8``), and sorted by a
structured key, not string padding: the leading integer, then any letter/range suffix,
so ``2`` < ``10``, ``7a`` < ``10``, and a range like ``441-450`` orders by its start.

NavPoints are sorted into four zones, in output order:
- **Zone 0**: title page (``classify_special_chapter(title) == "title"``).
- **Zone 1**: front-matter (prologue, preface, foreword, …; no chapter number).
- **Zone 2**: content chapters (with or without a chapter number).
- **Zone 3**: back-matter (epilogue, appendix, bonus, …; no chapter number).

Classification precedence: title wins over a number; a number wins over
front/back; anything left is content. The title→role classification itself is the shared
:func:`~ebookerr_sdk.epub.chapters.chapter_role` (``CHC-D5``); this module only maps a role to its
output zone. Within zone 2 (content):
- Chapters **with** a number are sorted by ``(_sort_key(number), original_index)`` and occupy
  exactly the slots numbered chapters originally held (relative to the zone-2 sub-sequence).
- Chapters **without** a number keep their own original slot, so an unnumbered chapter never
  crosses a numbered one.

Zones 0, 1, 3 keep their internal relative order (stable) and are emitted in zone order 0, 1, 2, 3.
Only when the desired order differs from the current one are the NCX navMap and OPF spine
rewritten — chapter files, their ``navLabel`` text and ``content`` src are never touched, and an
already-ordered book is a no-op (the EPUB is not rewritten).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

from ebookerr_sdk.domain.chapter_number import special_section_number
from ebookerr_sdk.epub import EpubDocument, NavPoint
from ebookerr_sdk.epub.chapter_numbers import nav_chapter_numbers
from ebookerr_sdk.epub.chapters import (
    ChapterRole,
    chapter_keys,
    chapter_role,
    classify_spine,
)
from ebookerr_sdk.epub.titles import resolve_book_title, resolve_nav_title

from epub_chapter_reorder.book_display import display_title as display_title_for

logger = logging.getLogger(__name__)


def automatic_key_order(doc: EpubDocument) -> list[str]:
    """Return the stable chapter keys in the order the automatic algorithm would produce.

    Reads the NCX navMap, derives chapter numbers, and returns the chapter keys in the
    order they would be arranged by the automatic reordering algorithm. This is used
    for comparison with stored manual orders to detect when a user submits the automatic
    order and should clear the stored memory.

    Args:
        doc: The EPUB document to analyze.

    Returns:
        A list of stable chapter keys (from chapter_keys) in automatic order. Empty
        strings are dropped from the result.
    """
    nav_points = doc.ncx.nav_points()
    if not nav_points:
        return []

    entries = classify_spine(doc)

    # Build mapping from entry idref to stable chapter key (unique per entry)
    key_by_entry = chapter_keys(doc, entries)

    # Build mapping from nav_id to key by walking nav points
    keys_by_nav: dict[str, str] = {}
    for point in nav_points:
        item = doc.opf.item_by_href(point.src) if point.src else None
        if item is not None and item.id in key_by_entry:
            keys_by_nav[point.id] = key_by_entry[item.id]
        else:
            keys_by_nav[point.id] = ""

    # Get the current nav order and validate uniqueness
    current = [p.id for p in nav_points]
    if not current or len(set(current)) != len(current):
        return []

    # Build titles and numbers mapping for the automatic order algorithm
    titles: dict[str, str] = {}
    numbers: dict[str, str] = {}
    for entry in entries:
        # Find the corresponding nav point for this entry
        for point in nav_points:
            item = doc.opf.item_by_href(point.src) if point.src else None
            if item is not None and item.id == entry.idref:
                titles[point.id] = entry.title
                numbers[point.id] = entry.number
                break

    # Compute automatic order using the shared algorithm
    automatic = _desired_order(current, titles, numbers)

    # Map automatic nav_ids to stable keys, dropping empty strings
    return [key for nav_id in automatic if (key := keys_by_nav.get(nav_id, ""))]


def sort_number(title: str, number: str) -> str:
    """Return the number that orders a chapter within its band, or ``""`` (``EXP-127``).

    A numbered special section (interlude, side story, etc.) has an ordinal that
    identifies the section itself, not a position in the chapter sequence. This function
    returns the sort number for a chapter: the number if it is not a numbered special
    section, or an empty string to prevent ordering by the special-section ordinal.

    Args:
        title: The chapter title.
        number: The chapter number string.

    Returns:
        The sort number to use for ordering, or ``""`` when the number belongs to
        a special-section phrase and should not participate in content-chapter sorting.
    """
    return "" if special_section_number(title) is not None else number


def _sort_key(number: str) -> tuple[int, str]:
    """Order by the leading integer, then any letter/range suffix (so ``7a`` < ``10``)."""
    match = re.match(r"(\d+)(.*)", number)
    if match:
        return int(match.group(1)), match.group(2)
    return (1 << 30, number)


_ZONE_BY_ROLE: dict[ChapterRole, int] = {
    ChapterRole.TITLE: 0,
    ChapterRole.FRONT: 1,
    ChapterRole.CONTENT: 2,
    ChapterRole.BACK: 3,
    ChapterRole.OTHER: 2,
}


def _desired_order(
    nav_ids: list[str], titles: dict[str, str], numbers: dict[str, str]
) -> list[str]:
    """Return ``nav_ids`` in output order.

    Zones 0, 1, 3 keep their internal relative order (stable) and are emitted in zone order.
    Inside zone 2, entries with a number are sorted by ``(_sort_key(number), original_index)``
    and re-placed into exactly the slots numbered entries originally occupied. Entries without
    a number keep their own slot — so an unnumbered chapter never crosses a numbered one.

    Zone assignment is via :func:`~ebookerr_sdk.epub.chapters.chapter_role`, which classifies
    each chapter from its title and number alone.
    """
    zones: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    for nav_id in nav_ids:
        zones[_ZONE_BY_ROLE[chapter_role(titles[nav_id], numbers[nav_id])]].append(nav_id)
    content = zones[2]
    slots = [i for i, nav_id in enumerate(content) if sort_number(titles[nav_id], numbers[nav_id])]
    ranked = sorted(
        slots,
        key=lambda i: (
            _sort_key(sort_number(titles[content[i]], numbers[content[i]])),
            i,
        ),
    )
    ordered_content = list(content)
    for slot, source in zip(slots, ranked, strict=True):
        ordered_content[slot] = content[source]
    return [*zones[0], *zones[1], *ordered_content, *zones[3]]


def _manual_desired_order(
    nav_ids: list[str],
    keys: dict[str, str],
    automatic: list[str],
    manual_order: Sequence[str],
) -> tuple[list[str], int, int]:
    """Return (order, matched, spliced) merging a stored key order with the automatic one.

    Implements the algorithm from the task specification, without zone awareness.
    The automatic order already respects zones; this function reorders entries
    to honor manual_order while splicing in new chapters at appropriate positions.

    Args:
        nav_ids: Current navPoint IDs in spine order.
        keys: Mapping from navPoint ID to rebuild-stable chapter key.
        automatic: The navPoint order from the automatic algorithm.
        manual_order: A stored list of stable chapter keys in desired order.

    Returns:
        A tuple of (order, matched, spliced) where:
        - order: The merged navPoint ID list
        - matched: Number of manual_order keys found in nav_ids
        - spliced: Number of chapters not in manual_order (newly added)
    """
    # 1. Build position map
    known = {key: position for position, key in enumerate(manual_order)}
    auto_index = {nav_id: i for i, nav_id in enumerate(automatic)}

    # 2. Partition nav_ids into placed and extra
    placed = []
    extra = []
    for nav_id in nav_ids:
        if keys[nav_id] in known:
            placed.append(nav_id)
        else:
            extra.append(nav_id)

    matched = len(placed)
    spliced = len(extra)

    # 3. Sort placed by known position, ties broken by automatic order
    placed_sorted = sorted(placed, key=lambda nav_id: (known[keys[nav_id]], auto_index[nav_id]))

    # 4 & 5. Merge extras at their anchor positions
    result = list(placed_sorted)
    for extra_id in extra:
        extra_auto_idx = auto_index[extra_id]
        # Find nearest earlier placed entry in automatic order
        anchor_idx = -1  # will insert at position 0 if no anchor found
        for i in range(extra_auto_idx - 1, -1, -1):
            if automatic[i] in placed:
                # Found the anchor in placed; find its position in result
                anchor_idx = result.index(automatic[i])
                break

        # Insert immediately after anchor (or at front if no anchor)
        result.insert(anchor_idx + 1, extra_id)

    return result, matched, spliced


def _spine_order(doc: EpubDocument, nav_points: list[NavPoint], desired: list[str]) -> list[str]:
    """Map the desired navPoint order to spine idrefs (via content src -> manifest)."""
    src_by_nav = {point.id: point.src for point in nav_points}
    order: list[str] = []
    for nav_id in desired:
        item = doc.opf.item_by_href(src_by_nav.get(nav_id, ""))
        if item is not None:
            order.append(item.id)
    return order


def reorder_epub(
    epub_path: Path,
    *,
    manual_order: Sequence[str] | None = None,
    display_title: str | None = None,
) -> bool:
    """Reorder EPUB chapters into chronological order; return True if the file changed.

    Reads the NCX navMap, derives chapter numbers via
    :func:`~ebookerr_sdk.epub.chapter_numbers.nav_chapter_numbers`, and rewrites the NCX navMap
    and OPF spine only when the order is wrong.  An already-ordered EPUB is a
    no-op (the file is not rewritten and False is returned).

    Args:
        epub_path: Path to the EPUB file to inspect and, if needed, rewrite in place.
        manual_order: A previously-stored list of stable chapter keys (``CHX-D4``). Applies
            only within the content zone (``CHC-D5``) — the title page, front matter and
            back matter always keep the automatic algorithm's zone placement. Within
            content, chapters whose key appears in it are emitted in its order, and any
            chapter whose key is unknown — added by a later pull — is spliced in at the
            position the automatic algorithm would give it relative to its nearest known
            neighbour, never appended blindly. A duplicate-key order is refused and logged
            as a refusal, falling through to the automatic order. Keys of title-page,
            front-matter and back-matter rows may appear in the stored order; they are
            counted as matched and placed by their zone.
        display_title: The book record's user-facing title (already override-merged). Used
            for every log line this function emits; the EPUB's own metadata title stands in
            only when this is ``None`` or blank, so a renamed book is never reported under
            a name the user cannot find.

    Returns:
        True if the EPUB was rewritten, False if it was already in order or the table
        of contents is unusable (has no navPoints or duplicate IDs); both cases log
        a message at INFO/WARNING level.
    """
    doc = EpubDocument.open(epub_path)
    book_title = display_title_for(display_title, epub_title=resolve_book_title(doc))
    nav_points = doc.ncx.nav_points()
    titles = {p.id: resolve_nav_title(doc, p) for p in nav_points}
    numbers = nav_chapter_numbers(doc)
    current = [p.id for p in nav_points]
    if not current or len(set(current)) != len(current):
        logger.warning(
            'Chapter-reorder skipped for "%s": table of contents has %d entry(ies) with '
            "%d distinct id(s) — nothing to reorder safely",
            book_title,
            len(current),
            len(set(current)),
        )
        return False

    automatic = _desired_order(current, titles, numbers)
    zone_counts = {
        z: sum(1 for n in current if _ZONE_BY_ROLE[chapter_role(titles[n], numbers[n])] == z)
        for z in (0, 1, 2, 3)
    }
    logger.debug(
        'Chapter-reorder decision for "%s": %d entry(ies), zones=%s',
        book_title,
        len(current),
        zone_counts,
    )
    logger.debug(
        'Chapter-reorder computed order for "%s": %d entry(ies), %d numbered',
        book_title,
        len(current),
        sum(1 for nav_id in current if sort_number(titles[nav_id], numbers[nav_id])),
    )

    # Refuse a duplicate-key order before applying it
    if manual_order is not None:
        distinct = len(set(manual_order))
        if distinct != len(manual_order):
            logger.warning(
                'Manual chapter order refused for "%s": %d key(s) but only %d distinct — the '
                "stored order cannot express an ordering and was ignored",
                book_title,
                len(manual_order),
                distinct,
            )
            manual_order = None

    # Apply manual order if provided
    if manual_order is not None:
        entries = classify_spine(doc)
        key_by_idref = chapter_keys(doc, entries)

        # Build mapping from navPoint id to chapter key
        src_by_nav = {point.id: point.src for point in nav_points}
        keys: dict[str, str] = {}
        for nav_id in current:
            item = doc.opf.item_by_href(src_by_nav.get(nav_id, ""))
            if item is not None:
                keys[nav_id] = key_by_idref.get(item.id, "")
            else:
                keys[nav_id] = ""

        # Manual ordering applies only within the content zone (CHC-D5): title page,
        # front matter and back matter always keep their automatic zone placement, so
        # a book with no stored key for them is never treated as a spliced-in "new" chapter.
        zone_of = {
            nav_id: _ZONE_BY_ROLE[chapter_role(titles[nav_id], numbers[nav_id])]
            for nav_id in current
        }
        content_current = [nav_id for nav_id in current if zone_of[nav_id] == 2]
        content_automatic = [nav_id for nav_id in automatic if zone_of[nav_id] == 2]

        # Stored keys that belong to zone-placed rows (title page, front and back matter) are
        # honoured by their zone, not by the merge (CHC-D5) — they count as matched, never as
        # partial (EXP-185).
        zone_placed_keys = {
            keys[nav_id] for nav_id in current if zone_of[nav_id] != 2 and keys[nav_id]
        }
        content_manual = [key for key in manual_order if key not in zone_placed_keys]
        content_desired, matched, spliced = _manual_desired_order(
            content_current, keys, content_automatic, content_manual
        )
        matched += sum(1 for key in manual_order if key in zone_placed_keys)
        content_iter = iter(content_desired)
        desired = [next(content_iter) if zone_of[nav_id] == 2 else nav_id for nav_id in automatic]

        # Log the result
        if spliced or matched != len(manual_order):
            logger.warning(
                'Manual chapter order partially applied for "%s": %d of %d key(s) matched, '
                "%d chapter(s) spliced",
                book_title,
                matched,
                len(manual_order),
                spliced,
            )
        else:
            logger.info('Manual chapter order applied to "%s": %d chapter(s)', book_title, matched)
    else:
        desired = automatic

    if desired == current:
        logger.info(
            'Chapter-reorder left "%s" unchanged: %d navPoint(s) already in order',
            book_title,
            len(current),
        )
        return False

    doc.ncx.reorder(desired)
    doc.opf.reorder_spine(_spine_order(doc, nav_points, desired))
    doc.save()
    moved = sum(1 for before, after in zip(current, desired, strict=True) if before != after)
    logger.info(
        'Chapter-reorder fixed %d of %d navPoint(s) in "%s"', moved, len(current), book_title
    )
    return True
