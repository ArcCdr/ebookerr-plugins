"""EPUB3 landmarks and EPUB2 guide entries for jumping past front matter.

A landmark (EPUB3) or guide entry (EPUB2) with ``type="text"`` or ``epub:type="bodymatter"``
allows a reading system to jump directly to where the story actually starts, bypassing
front matter like title pages and prologues.

Front-matter chapters (prologue, preface, foreword) count as narrative content because
readers may want to read them sequentially; they are only skipped if they are the very
first content. The title page and cover, by contrast, are always non-narrative.

Reference: ``MR-NAV-1``, ``SH-19``, ``AT-NAV-1``, ``AT-NAV-2``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ebookerr_sdk.domain.chapter_number import classify_special_chapter
from ebookerr_sdk.epub.builder import LandmarkEntry
from ebookerr_sdk.epub.roles import NON_CHAPTER_ITEM_IDS

from epub_merge.merge.model import PlannedChapter

logger = logging.getLogger(__name__)


def first_narrative_href(chapters: Sequence[PlannedChapter]) -> str | None:
    """The filename of the first chapter that is real narrative content, or ``None``.

    The first narrative chapter is the first chapter whose:
    - ``item_id.lower()`` is NOT in :const:`NON_CHAPTER_ITEM_IDS`, AND
    - ``classify_special_chapter(chapter.label)`` is NOT ``"title"``.

    Front-matter chapters (prologue, preface, etc.) that are not title pages count
    as narrative content.

    Args:
        chapters: Chapters in reading order.

    Returns:
        The filename of the first narrative chapter, or None if all chapters are
        front matter.
    """
    for chapter in chapters:
        # Skip chapters explicitly marked as non-chapters by item_id
        if chapter.item_id.lower() in NON_CHAPTER_ITEM_IDS:
            continue

        # Skip title pages (but not other special chapters like prologue/epilogue)
        classification = classify_special_chapter(chapter.label)
        if classification == "title":
            continue

        # This is narrative content
        return chapter.filename

    return None


def build_landmarks(
    chapters: Sequence[PlannedChapter], *, cover_href: str | None
) -> tuple[LandmarkEntry, ...]:
    """Build the EPUB3 landmarks list: cover, titlepage, toc and bodymatter.

    Emits landmarks in this order, skipping any whose target is unknown:
    1. Cover (when ``cover_href`` is given, pointing at that exact page)
    2. Title page (when a chapter's ``classify_special_chapter(label) == "title"``)
    3. Table of contents (always present as ``#toc``)
    4. Start of content (when ``first_narrative_href`` is not None)

    Args:
        chapters: Chapters in reading order.
        cover_href: The merged book's cover page path, or None when it has no cover.

    Returns:
        Tuple of landmark entries in the order to appear in the nav.
    """
    landmarks: list[LandmarkEntry] = []

    # Add cover landmark if present, pointing at its actual (possibly moved) path
    if cover_href:
        landmarks.append(LandmarkEntry("cover", "Cover", cover_href))

    # Add title page landmark if one exists
    for chapter in chapters:
        if classify_special_chapter(chapter.label) == "title":
            landmarks.append(LandmarkEntry("titlepage", "Title Page", chapter.filename))
            break

    # Always add TOC landmark
    landmarks.append(LandmarkEntry("toc", "Table of Contents", "#toc"))

    # Add bodymatter (start of content) landmark
    narrative_href = first_narrative_href(chapters)
    if narrative_href is not None:
        landmarks.append(LandmarkEntry("bodymatter", "Start of Content", narrative_href))
        logger.debug("Merge start-of-content target: %s", narrative_href)

    # Log the landmarks we're emitting
    logger.debug("Merge landmarks: %s", [entry.epub_type for entry in landmarks])

    return tuple(landmarks)
