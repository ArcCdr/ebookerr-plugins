"""Apply canonical stylesheet set across merged chapters (``MR-CSS-1``).

When multiple books are merged, their stylesheets conflict. Rather than attempting
to merge CSS files (which is error-prone and complex), this module adopts the
survivor's stylesheet set as canonical: every non-survivor chapter is repointed to
use the same stylesheets, in the same order, as the survivor.

Design rationale (``MR-CSS-1``): merged chapters come from one series and are
expected to share consistent visual presentation. Adopting one stylesheet set
is simpler and safer than merging CSS.

Rules enforced:
1. Only the survivor's stylesheets survive (``MR-CSS-1`` rule 1–2).
2. Non-survivor chapters are relinked to the canonical set (``MR-CSS-1`` rule 2).
3. Non-survivor chapters with no stylesheet link remain unchanged (``MR-CSS-1`` rule 3).
4. Survivor chapters are never touched (``MR-CSS-1`` rule 4).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from ebookerr_sdk.epub.xhtml import ChapterDocument

from epub_merge.merge.assets import AssetPlan
from epub_merge.merge.model import InputBook, PlannedChapter

logger = logging.getLogger(__name__)


def canonical_stylesheets(books: Sequence[InputBook], assets: AssetPlan) -> list[str]:
    """Derive the survivor's stylesheets as output paths (``MR-CSS-1`` rules 1–2).

    For each stylesheet href in the survivor's (books[0]) stylesheet list,
    resolves it through the asset mapping to find its output path. Missing
    entries (not in assets.mapping) are skipped.

    Args:
        books: Input books in merge order; books[0] is the survivor.
        assets: Planned resource set with mapping of (book_index, input_href) -> output_path.

    Returns:
        A list of output stylesheet hrefs in the survivor's order, omitting any
        unmapped entries.
    """
    if not books:
        return []

    survivor = books[0]
    result: list[str] = []

    for href in survivor.stylesheets:
        output_href = assets.mapping.get((0, href))
        if output_href is not None:
            result.append(output_href)

    return result


def apply_canonical_stylesheets(
    chapters: Sequence[PlannedChapter], canonical: Sequence[str]
) -> list[PlannedChapter]:
    """Re-point every non-survivor chapter's stylesheet links at ``canonical``.

    Processes each chapter in order:
    - Survivor chapters (book_index == 0) are left untouched.
    - Non-survivor chapters are parsed and have their stylesheet links replaced
      via ``set_stylesheets(canonical)``. If set_stylesheets returns False
      (indicating no stylesheet link was present), the chapter is returned unchanged
      per ``MR-CSS-1`` rule 3.
    - Chapters that fail to parse are logged at WARNING and returned unchanged.

    When ``canonical`` is empty, non-survivor chapters have their stylesheet links
    **removed** (there is no stylesheet to point at, and SH-11 forbids dangling links).

    Args:
        chapters: Planned chapters to process (in reading order).
        canonical: Output stylesheet hrefs to apply to non-survivor chapters.

    Returns:
        A list of PlannedChapter with non-survivor stylesheet links potentially
        rewritten, in the same order.
    """
    result: list[PlannedChapter] = []
    changed_count = 0

    logger.debug("Merge canonical stylesheet set: %s", canonical)

    for chapter in chapters:
        # Survivor chapters are never touched
        if chapter.book_index == 0:
            result.append(chapter)
            continue

        # Try to relink this non-survivor chapter
        relinked_chapter, was_changed = _relink_one(chapter, canonical)
        result.append(relinked_chapter)
        if was_changed:
            changed_count += 1

    logger.info(
        "Merge relinked %d non-survivor chapter(s) to %d canonical stylesheet(s)",
        changed_count,
        len(canonical),
    )

    return result


def _relink_one(chapter: PlannedChapter, canonical: Sequence[str]) -> tuple[PlannedChapter, bool]:
    """Relink stylesheet references in a single non-survivor chapter.

    Parses the chapter's XHTML, applies set_stylesheets(canonical), and if the
    operation succeeds and changes the document, replaces the chapter's xhtml bytes.

    Args:
        chapter: The non-survivor chapter to relink.
        canonical: Output stylesheet hrefs to apply.

    Returns:
        A tuple of (relinked chapter, was_changed). The chapter's xhtml is replaced
        with parsed and modified bytes if set_stylesheets succeeded and made changes,
        otherwise the original bytes are returned.
    """
    try:
        document = ChapterDocument.parse(chapter.xhtml.decode("utf-8"))
        changed = document.set_stylesheets(canonical)

        if changed:
            logger.debug('Merge relinked stylesheet in "%s"', chapter.filename)
            new_xhtml = document.to_xml().encode("utf-8")
            return replace(chapter, xhtml=new_xhtml), True
        else:
            # No stylesheet link was present (per set_stylesheets contract)
            return chapter, False

    except Exception as exc:
        logger.warning(
            'Merge could not relink stylesheet in "%s": %s; keeping it unchanged',
            chapter.filename,
            exc,
        )
        return chapter, False
