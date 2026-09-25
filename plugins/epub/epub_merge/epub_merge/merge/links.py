"""Rewrite chapter XHTML references after flattening (``MR-FLAT-1``).

When `plan_chapters` flattens every book's XHTML documents to the merged book's
OPF directory root, all relative paths within those documents break. For example,
a chapter at `OEBPS/text/ch1.xhtml` containing `<img src="../images/divider.png">`
now sits at the root with `../images/divider.png` pointing nowhere.

This module fixes those references, repointing every `<link href>`, `<img src>`,
`<a href>`, and similar attributes to their new locations in the merged book.
Only in-document references are touched; document formatting is never altered.

A chapter that cannot be parsed is passed through unchanged rather than failing
the merge, allowing a merge to succeed with one malformed chapter.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Sequence
from dataclasses import replace

from ebookerr_sdk.epub.xhtml import ChapterDocument

from epub_merge.merge.assets import AssetPlan
from epub_merge.merge.model import PlannedChapter

logger = logging.getLogger(__name__)


def rewrite_chapter_links(
    chapters: Sequence[PlannedChapter], assets: AssetPlan
) -> list[PlannedChapter]:
    """Repoint every chapter's internal references at their new locations (``MR-FLAT-1``).

    For each chapter, resolves every relative reference (image, stylesheet, cross-chapter link)
    against:
    1. The chapter's original directory (base path computed from source_href).
    2. The merged book's asset mapping (which may have renamed or re-based assets).
    3. Cross-chapter links via the filename of the target chapter in the merge.

    A chapter that cannot be parsed is returned unchanged and logged at WARNING.

    Args:
        chapters: Planned chapters to rewrite (in reading order).
        assets: Planned resource set with mapping of (book_index, input_href) -> output_path.

    Returns:
        A list of PlannedChapter with xhtml potentially rewritten, in the same order.
    """
    # Build a map of (book_index, source_href) -> planned filename for cross-chapter links
    chapter_by_href = {(c.book_index, c.source_href): c.filename for c in chapters}

    result: list[PlannedChapter] = []
    changed_count = 0

    for chapter in chapters:
        rewritten_chapter, was_changed = _rewrite_one(chapter, chapter_by_href, assets)
        result.append(rewritten_chapter)
        if was_changed:
            changed_count += 1

    logger.info("Merge rewrote references in %d of %d chapter(s)", changed_count, len(chapters))
    return result


def _rewrite_one(
    chapter: PlannedChapter,
    chapter_by_href: dict[tuple[int, str], str],
    assets: AssetPlan,
) -> tuple[PlannedChapter, bool]:
    """Rewrite references in a single chapter.

    Args:
        chapter: The chapter to rewrite.
        chapter_by_href: Map of (book_index, source_href) -> planned filename.
        assets: Asset mapping for resource resolution.

    Returns:
        A tuple of (rewritten chapter, was_changed). The chapter's xhtml is replaced
        with parsed and rewritten bytes if any href changed, otherwise the original
        bytes are returned unchanged.
    """
    try:
        # Compute the base directory for relative reference resolution
        base = posixpath.dirname(chapter.source_href)

        def _resolve(reference: str) -> str | None:
            """Map one in-document reference to its merged-book path.

            Resolves the reference relative to the chapter's original directory,
            then checks: (1) if it's a cross-chapter link, (2) if it's an asset,
            or (3) leaves it unmapped.

            Args:
                reference: A local, unquoted href or src value (without fragment).

            Returns:
                The new path in the merged book, or None if unmapped.
            """
            # Resolve relative to the chapter's original directory
            target = (
                posixpath.normpath(posixpath.join(base, reference))
                if base
                else posixpath.normpath(reference)
            )

            # Check: is this a cross-chapter link?
            mapped_chapter = chapter_by_href.get((chapter.book_index, target))
            if mapped_chapter is not None:
                return mapped_chapter

            # Check: is this an asset?
            return assets.mapping.get((chapter.book_index, target))

        # Parse and rewrite
        document = ChapterDocument.parse(chapter.xhtml.decode("utf-8"))
        changed = document.rewrite_hrefs(_resolve)

        if changed:
            logger.debug('Merge rewrote references in "%s"', chapter.filename)
            new_xhtml = document.to_xml().encode("utf-8")
            return replace(chapter, xhtml=new_xhtml), True
        else:
            return chapter, False

    except Exception as exc:
        logger.warning(
            'Merge could not rewrite references in "%s": %s; keeping it unchanged',
            chapter.filename,
            exc,
        )
        return chapter, False
