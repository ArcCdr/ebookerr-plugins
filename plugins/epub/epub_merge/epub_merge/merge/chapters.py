"""Plan the flat, renamed chapter set for EPUB merge (``MR-FLAT-1``, ``MR-RENAME-1``).

This module is pure (models in, models out). It flattens every input book's chapters
into one renamed, collision-free list, preserving input order per §7. Chapter renaming
is delegated to :func:`ebookerr_sdk.domain.chapter_naming.plan_chapter_filenames` so the merge
and any future renamer share one algorithm. XHTML bytes are carried through unchanged
at this stage — later steps rewrite references and stylesheet links.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence

from ebookerr_sdk.domain.chapter_naming import plan_chapter_filenames

from epub_merge.merge.model import InputBook, PlannedChapter

logger = logging.getLogger(__name__)


def plan_chapters(
    books: Sequence[InputBook], *, reserved: Collection[str] = ()
) -> list[PlannedChapter]:
    """Flatten every book's chapters into one renamed, collision-free list (input order).

    Algorithm:
    1. Concatenate book.chapters across books in books order, each book's chapters
       in their own order (ordering is preserved, never sorted).
    2. Feed every chapter's label to plan_chapter_filenames in that same order.
    3. Emit one PlannedChapter per input chapter, in the same order.

    Args:
        books: InputBooks in merge order (survivor first, then sources).
        reserved: Stems already taken by something outside this call, forwarded
            to :func:`ebookerr_sdk.domain.chapter_naming.plan_chapter_filenames`.

    Returns:
        A list of PlannedChapter, one per input chapter, in input order.
        Filenames are collision-free and semantic (e.g. "chapter_174_three_square_meals.xhtml").

    Raises:
        None (pure function; all validation belongs upstream).
    """
    # Step 1: Collect (book_index, chapter) pairs in order
    chapters_with_book_index: list[tuple[int, int, str]] = []
    total_chapters = 0
    for book in books:
        for chapter in book.chapters:
            chapters_with_book_index.append((book.index, total_chapters, chapter.label))
            total_chapters += 1

    if not chapters_with_book_index:
        return []

    # Step 2: Extract labels in order and plan filenames
    labels = [label for _, _, label in chapters_with_book_index]
    named_chapters = plan_chapter_filenames(labels, reserved=reserved)

    # Step 3: Build PlannedChapter objects by zipping with original chapters
    planned: list[PlannedChapter] = []
    chapter_index = 0

    for book in books:
        for chapter in book.chapters:
            position = chapter_index + 1
            named = named_chapters[chapter_index]
            book_index = book.index

            logger.debug(
                'Merge chapter %d/%d from input %d: "%s" -> %s',
                position,
                total_chapters,
                book_index,
                chapter.label,
                named.filename,
            )

            planned.append(
                PlannedChapter(
                    label=chapter.label,
                    filename=named.filename,
                    item_id=named.stem,
                    number=named.number,
                    book_index=book_index,
                    source_href=chapter.href,
                    xhtml=chapter.xhtml,
                )
            )
            chapter_index += 1

    logger.info("Merge planned %d chapter(s) from %d book(s)", len(planned), len(books))

    return planned
