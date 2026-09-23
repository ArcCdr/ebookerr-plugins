"""Deterministic survivor election, owned by the merge plugin.

Survivor election is merge knowledge — the decision of which book survives when
multiple EPUB files are merged. It belongs with the merge service, not in the
core adoption domain. The choice is deterministic and order-independent: shuffling
the candidates cannot change the answer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ebookerr_sdk.domain.adoption import expand_number
from ebookerr_sdk.epub.chapters import ChapterRole, classify_spine
from ebookerr_sdk.epub.document import EpubDocument
from ebookerr_sdk.epub.errors import MalformedEpubError

logger = logging.getLogger(__name__)

_NONE_SENTINEL = 10**9
"""Sentinel value to sort None last in numerical comparisons."""


@dataclass(frozen=True, slots=True)
class SurvivorCandidate:
    """One book offered to the survivor election, with everything the rule needs already read.

    Attributes:
        book_id: The book's id; the final, total tie-break.
        title: The book's title, for logging only — never scored.
        first_number: The lowest integer the book's **first** content chapter covers, or
            ``None`` when that chapter carries no parsable number.
        min_number: The lowest integer any of the book's chapters covers, or ``None``.
        chapter_count: How many chapters the book packages.
        created_at: When the row was created; the fourth tie-break.
    """

    book_id: str
    title: str
    first_number: int | None
    min_number: int | None
    chapter_count: int
    created_at: datetime | None


def elect_survivor(candidates: Sequence[SurvivorCandidate]) -> str:
    """Elect the book a merge should keep, from the books themselves.

    The survivor is the **start** of the work — the book holding the earliest chapters ebookerr
    actually has, not the earliest chapters that exist. The key, in order:

    1. ``first_number`` ascending — the lowest number the book's first content chapter covers.
       A book whose first chapter carries no number sorts last on this facet.
    2. ``min_number`` ascending — the lowest number anywhere in the book, same rule.
    3. ``chapter_count`` **descending** — on an otherwise even tie, the book that already holds
       more of the work keeps its identity.
    4. ``created_at`` ascending — the older row wins; ``None`` sorts last.
    5. ``book_id`` ascending — a total order, so the result never depends on input order.

    This is a pure function of the **set**: shuffling ``candidates`` cannot change the answer.
    That is the whole point — 2.18.27 elected ``items[0]``, i.e. whichever book the user
    happened to click first.

    Args:
        candidates: Every book in the merge, in any order.

    Returns:
        The elected survivor's ``book_id``.

    Raises:
        ValueError: When ``candidates`` is empty.
    """
    if not candidates:
        raise ValueError("No candidates to elect from")

    # Sort with None values sorting last via sentinel
    def sort_key(
        c: SurvivorCandidate,
    ) -> tuple[int, int, int, tuple[int, datetime | None], str]:
        """Generate sort key for a candidate."""
        # first_number: ascending, None sorts last
        first_num = c.first_number if c.first_number is not None else _NONE_SENTINEL
        # min_number: ascending, None sorts last
        min_num = c.min_number if c.min_number is not None else _NONE_SENTINEL
        # chapter_count: descending (negate to reverse)
        chapter_count = -c.chapter_count
        # created_at: ascending, None sorts last
        created_at_sort = (0, c.created_at) if c.created_at is not None else (1, None)
        # book_id: ascending (lexicographic)
        return (first_num, min_num, chapter_count, created_at_sort, c.book_id)

    winner = min(candidates, key=sort_key)

    logger.info(
        (
            'Merge survivor elected: "%s" (book_id=%s) from %d candidate(s) — '
            "first=%s, min=%s, chapters=%d"
        ),
        winner.title,
        winner.book_id,
        len(candidates),
        winner.first_number,
        winner.min_number,
        winner.chapter_count,
    )

    if candidates[0].book_id != winner.book_id:
        logger.info(
            (
                "Merge survivor differs from the caller's first book: "
                "elected book_id=%s, offered book_id=%s"
            ),
            winner.book_id,
            candidates[0].book_id,
        )

    return winner.book_id


def candidate_from_epub(
    book_id: str, title: str, epub_path: Path, created_at: datetime | None
) -> SurvivorCandidate:
    """Build a candidate by reading a book's EPUB.

    ``first_number`` and ``min_number`` come from ``classify_spine`` entries whose role is
    ``ChapterRole.CONTENT``, expanded through
    :func:`~ebookerr_sdk.domain.adoption.expand_number`; ``chapter_count`` is
    :meth:`~ebookerr_sdk.epub.document.EpubDocument.content_chapter_count`.

    An unreadable EPUB yields a candidate with ``first_number=None``, ``min_number=None`` and
    ``chapter_count=0``, logged at WARNING — it can still be elected, but only if nothing else
    can be, which is the fail-closed answer.

    Args:
        book_id: The book's database id.
        title: The book's title for logging.
        epub_path: Path to the EPUB file.
        created_at: When the book row was created, or None.

    Returns:
        A SurvivorCandidate with the extracted EPUB metadata.
    """
    try:
        doc = EpubDocument.open(epub_path)
    except (MalformedEpubError, Exception):
        logger.warning(
            "Could not read EPUB at %s for survivor candidate book_id=%s",
            epub_path,
            book_id,
        )
        return SurvivorCandidate(
            book_id=book_id,
            title=title,
            first_number=None,
            min_number=None,
            chapter_count=0,
            created_at=created_at,
        )

    # Classify all chapters
    chapters = classify_spine(doc)

    # Extract content chapters only
    content_chapters = [c for c in chapters if c.role == ChapterRole.CONTENT]

    if not content_chapters:
        # No content chapters found
        return SurvivorCandidate(
            book_id=book_id,
            title=title,
            first_number=None,
            min_number=None,
            chapter_count=0,
            created_at=created_at,
        )

    # Get first_number from the first content chapter
    first_chapter = content_chapters[0]
    first_numbers = expand_number(first_chapter.number)
    first_number = min(first_numbers) if first_numbers else None

    # Get min_number from all content chapters
    all_numbers: set[int] = set()
    for chapter in content_chapters:
        all_numbers.update(expand_number(chapter.number))
    min_number = min(all_numbers) if all_numbers else None

    # Get chapter count from the document
    chapter_count = doc.content_chapter_count()

    return SurvivorCandidate(
        book_id=book_id,
        title=title,
        first_number=first_number,
        min_number=min_number,
        chapter_count=chapter_count,
        created_at=created_at,
    )
