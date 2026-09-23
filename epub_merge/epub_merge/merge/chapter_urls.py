"""Stamp a chapter URL into every merged chapter that lacks one (merge policy).

A merged book is only as recognisable as the URLs its chapters declare. Two rules decide what
each input book contributes:

* **One-chapter book** — that chapter *is* the book, so the book's own URL is stamped onto it,
  falling back to whatever URL the chapter already declared when the book URL is unknown.
* **Multi-chapter book** — if any chapter declares a URL, every chapter is left exactly as it
  is (the per-chapter URLs are real and must be preserved). Only when *none* of them declares
  one is the book URL stamped onto all of them, so the merged book still points somewhere
  meaningful.

Filling individual gaps in a partially-annotated book is deliberately not done: that chapter is
not at the book URL, and guessing would record a wrong address.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from ebookerr_sdk.epub.errors import MalformedEpubError
from ebookerr_sdk.epub.roles import TITLE_PAGE_ITEM_IDS
from ebookerr_sdk.epub.xhtml import ChapterDocument, TitlePageDocument

from epub_merge.merge.model import InputBook, PlannedChapter

logger = logging.getLogger(__name__)


def stamp_chapter_urls(
    chapters: Sequence[PlannedChapter],
    books: Sequence[InputBook],
    book_urls: Sequence[str | None] = (),
) -> list[PlannedChapter]:
    """Declare ``<meta name="chapterurl">`` on merged chapters that lack one.

    Args:
        chapters: The planned chapters, in merge order.
        books: The input books, in merge order; ``book.index`` links a chapter back to its book.
        book_urls: Caller-supplied book URLs aligned with *books* by position (the core's
            ``story_url`` for each input). A missing or ``None`` entry falls back to the URL
            the input EPUB itself declares.

    Returns:
        A list of ``PlannedChapter`` in the same order, with ``xhtml`` replaced only for the
        chapters that were actually stamped.
    """
    # Build index: book_index -> list of chapter positions
    by_index: dict[int, list[int]] = {}
    for pos, chapter in enumerate(chapters):
        if chapter.book_index not in by_index:
            by_index[chapter.book_index] = []
        by_index[chapter.book_index].append(pos)

    result = list(chapters)  # Start with the original chapters
    total_stamped = 0

    for book in books:
        url = _book_url(book, book_urls)
        positions = by_index.get(book.index, [])

        if not positions:
            continue

        stamped_count = _process_book(book, url, chapters, positions, result)
        total_stamped += stamped_count

    logger.info(
        "Merge stamped chapter URLs on %d of %d chapter(s)",
        total_stamped,
        len(chapters),
    )
    return result


def _process_book(
    book: InputBook,
    url: str | None,
    chapters: Sequence[PlannedChapter],
    positions: list[int],
    result: list[PlannedChapter],
) -> int:
    """Process one book's chapters for URL stamping.

    Args:
        book: The input book being processed.
        url: The resolved book URL to stamp (or None).
        chapters: The original planned chapters.
        positions: The chapter positions belonging to this book.
        result: The result list to update in-place.

    Returns:
        The number of chapters that were stamped in this book.
    """
    # Filter out title page by manifest id (CHC-D9) from positions for single vs multi-chapter logic
    content_positions = [
        pos for pos in positions if chapters[pos].item_id.lower() not in TITLE_PAGE_ITEM_IDS
    ]

    # Parse each chapter to see if it has a chapterurl
    declared_urls: dict[int, str | None] = {}
    for pos in positions:
        chapter = chapters[pos]
        try:
            doc = ChapterDocument.parse(chapter.xhtml.decode("utf-8"))
            declared_urls[pos] = doc.meta("chapterurl")
        except (MalformedEpubError, Exception):
            # Parse error: chapter counts as declaring nothing
            declared_urls[pos] = None

    # Decide what to do (based on content chapters only, excluding title page)
    if len(content_positions) == 1:
        # One content chapter: stamp it when url is truthy
        pos = content_positions[0]
        stamped_chapter, was_stamped = _stamp_one(chapters[pos], url)
        result[pos] = stamped_chapter
        book_stamped = 1 if was_stamped else 0
    elif len(content_positions) > 1:
        # Multiple content chapters: check if any declared a URL
        any_declared = any(declared_urls[pos] is not None for pos in content_positions)
        if not any_declared and url:
            # None declared and we have a URL: stamp all content chapters
            book_stamped = 0
            for pos in content_positions:
                stamped_chapter, was_stamped = _stamp_one(chapters[pos], url)
                result[pos] = stamped_chapter
                if was_stamped:
                    book_stamped += 1
        else:
            book_stamped = 0
    else:
        # No content chapters (only title page)
        book_stamped = 0

    # Log per book
    if book_stamped > 0:
        logger.debug(
            'Merge stamped %d chapter URL(s) from input %d ("%s") with %s',
            book_stamped,
            book.index,
            book.title,
            url,
        )

    return book_stamped


def _book_url(book: InputBook, book_urls: Sequence[str | None]) -> str | None:
    """Resolve the URL to stamp onto a book's chapters.

    The caller's entry (when present and truthy) wins, else the EPUB's dc:source
    (when it's an http/https URL), else the title page's story URL, else None.

    Args:
        book: The input book.
        book_urls: Caller-supplied book URLs aligned with books by position.

    Returns:
        The resolved URL, or None.
    """
    # Check caller-supplied URL
    if book.index < len(book_urls) and book_urls[book.index]:
        return book_urls[book.index]

    # Check dc:source
    if book.source and (book.source.startswith("http://") or book.source.startswith("https://")):
        return book.source

    # Check title page
    if book.title_page:
        try:
            tp_doc = TitlePageDocument.parse(book.title_page.xhtml.decode("utf-8"))
            return tp_doc.story_url()
        except (MalformedEpubError, Exception) as exc:  # noqa: S110
            logger.debug("Could not read title page URL: %s", exc)

    return None


def _stamp_one(chapter: PlannedChapter, url: str | None) -> tuple[PlannedChapter, bool]:
    """Stamp a chapter URL onto a single chapter.

    Parses the chapter XHTML, sets the chapterurl meta if needed, and returns
    the chapter with updated xhtml (or the original if no change).

    Args:
        chapter: The chapter to potentially stamp.
        url: The URL to stamp, or None to leave the chapter alone.

    Returns:
        A tuple of (chapter, was_changed). The chapter is returned with potentially
        updated xhtml; was_changed is True only if the xhtml was actually modified.
    """
    if not url:
        return chapter, False

    try:
        doc = ChapterDocument.parse(chapter.xhtml.decode("utf-8"))
        changed = doc.set_meta("chapterurl", url)
        if changed:
            new_xhtml = doc.to_xml().encode("utf-8")
            return replace(chapter, xhtml=new_xhtml), True
        else:
            return chapter, False
    except (MalformedEpubError, Exception):
        # Parse error: return unchanged
        return chapter, False
