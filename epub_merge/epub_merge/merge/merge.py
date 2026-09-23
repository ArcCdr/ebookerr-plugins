"""Rebuild orchestrator for EPUB merge: read → plan → render → verify.

This module coordinates the merge of multiple EPUBs by reading their input
books into a neutral model, building a unified merge plan, rendering it to
disk, and verifying the result before committing it. Every step validates
its own contract, and the whole operation is atomic from the caller's
perspective: either the target EPUB is successfully replaced with the merged
result, or it remains untouched and a detailed error is raised.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from ebookerr_sdk.domain.adoption import expand_number
from ebookerr_sdk.domain.chapter_number import extract_chapter_info
from ebookerr_sdk.epub import EpubDocument, EpubError
from ebookerr_sdk.epub.chapters import ChapterRole
from ebookerr_sdk.epub.roles import NON_CHAPTER_ITEM_IDS

from epub_merge.merge.errors import (
    MergeStructureError,
)
from epub_merge.merge.model import InputBook, MergedChapter, MergeOptions, MergeOutcome
from epub_merge.merge.plan import MergePlan, build_merge_plan
from epub_merge.merge.reader import read_input_book
from epub_merge.merge.render import render_plan
from epub_merge.merge.validate import validate_plan

logger = logging.getLogger(__name__)


def _compute_duplicate_numbers(plan: MergePlan) -> tuple[str, ...]:
    """Chapter-number tokens that appear on more than one chapter in the plan.

    Extracts each planned chapter's number token from its label via
    ``extract_chapter_info``, expands it through ``expand_number``, and reports
    every individual number that appears in more than one chapter's expansion.
    A chapter with no parsable number never collides. Numbers are sorted numerically
    so that 2 precedes 10.

    Args:
        plan: The merge plan whose chapters to check.

    Returns:
        A sorted tuple of individual chapter numbers (as strings) that appear in
        more than one chapter.
    """
    # Map: individual integer -> count of chapters that contain this number
    number_to_count: dict[int, int] = {}

    for chapter in plan.chapters:
        # Extract chapter number token from the label
        info = extract_chapter_info(chapter.label)
        token = info.chapter_number
        if token:
            expanded = expand_number(token)
            for num in expanded:
                number_to_count[num] = number_to_count.get(num, 0) + 1

    # Find individual numbers that appear in more than one chapter
    duplicate_numbers = sorted([num for num, count in number_to_count.items() if count > 1])

    return tuple(str(num) for num in duplicate_numbers)


def _content_chapter_indices(book: InputBook) -> tuple[int, ...]:
    """The positions in ``book.chapters`` that count as chapters (``CHC-D1``).

    One rule for the whole product: a spine document counts unless
    :func:`~ebookerr_sdk.epub.chapters.classify_spine` gives it ``ChapterRole.OTHER`` — the
    auto-generated title page, a cover document, FanFicFare's ``log_page``, an EPUB3 nav
    document, a ``linear="no"`` entry, or anything the OPF ``<guide>`` declares as front or
    back matter. ``TITLE``, ``FRONT``, ``CONTENT`` and ``BACK`` all count, which is exactly what
    :meth:`~ebookerr_sdk.epub.document.EpubDocument.content_chapter_count` counts and therefore
    exactly what a book's stored ``num_chapters`` and its Chapter details row count already are.

    Supersedes 2.18.27's local rule, which additionally skipped any chapter whose *label* read
    as a title-page marker and so disagreed with every other count in the product by one.

    Args:
        book: The input book to extract content chapter indices from.

    Returns:
        A tuple of 0-based indices in ``book.chapters`` whose role is not ``ChapterRole.OTHER``.
    """
    indices = []
    for i, chapter in enumerate(book.chapters):
        if chapter.role != str(ChapterRole.OTHER):
            indices.append(i)
    return tuple(indices)


def merge_epubs(
    target: Path,
    sources: list[Path],
    options: MergeOptions | None = None,
    *,
    book_urls: list[str | None] | None = None,
) -> MergeOutcome:
    """Rebuild ``target`` as the merge of itself and ``sources``; return the outcome.

    Reads every input EPUB, builds a unified merge plan, renders the merged
    result to a temporary sibling file, verifies the output by reopening it,
    and atomically replaces the target. If any step fails, the target EPUB
    remains untouched on disk.

    Args:
        target: Path to the EPUB that receives every source's chapters;
            opened, planned, and replaced in place — but only after every
            input source has been validated and the merged output has been
            verified.
        sources: Paths to the EPUBs merged into ``target``, in order.
        options: Merge configuration (rewrite flags, cover, metadata). If not
            provided, defaults to ``MergeOptions()`` (no rewrites).
        book_urls: The core's own story URL for each input, aligned by position with
            ``[target, *sources]``. Used to stamp a chapter URL onto merged chapters that
            declare none; a missing or ``None`` entry falls back to the URL the input EPUB
            itself declares.

    Returns:
        The merged survivor's packaged chapter count and, when
        ``options.rewrite_book_title`` produced a rename, the title the merge
        wrote — never a title read back from the file (EXP-205).

    Raises:
        MergeInputError: If any input EPUB (target or source) cannot be read,
            is malformed, or has missing structure the merge requires. The
            target is not modified on disk.
        MergeContentError: If the merge plan produces fewer chapters than
            expected from the input books. The target is not modified on disk.
        MergeStructureError: If the merged output fails the merge's own
            structural self-check (e.g., reopening the file fails). The
            rejected output is moved to a temporary directory and the target
            is not modified on disk.
    """
    started = time.monotonic()
    logger.info('EPUB merge started: target="%s", %d source(s)', target.name, len(sources))

    # Step 1: Read all input books (target first)
    books = [read_input_book(path, index) for index, path in enumerate([target, *sources])]

    # Step 2: Compute expected chapter count from inputs
    expected = sum(len(_content_chapter_indices(b)) for b in books)
    logger.debug(
        "EPUB merge expects %d content chapter(s) across %d input(s): %s",
        expected,
        len(books),
        ", ".join(str(len(_content_chapter_indices(b))) for b in books),
    )

    # Step 3: Build the merge plan
    opts = options or MergeOptions()
    plan = build_merge_plan(books, opts, book_urls=book_urls or ())

    # Step 4: Validate the plan's structural invariants before writing
    validate_plan(plan, expected_chapters=expected)

    # Step 5: Write and verify
    total = _write_and_verify(plan, target, started)
    renamed = plan.title if opts.rewrite_book_title and plan.title != books[0].title else None

    # Compute contributions: count content chapters per input in plan order
    # using the same rule as _content_chapter_indices.
    contributions: dict[int, int] = {}
    for i, book in enumerate(books):
        contributions[i] = len(_content_chapter_indices(book))

    # Build contributions tuple in book order (survivor first)
    contributions_tuple = tuple(contributions.get(i, 0) for i in range(len(books)))

    # Compute duplicate chapter numbers from the planned chapters (R17)
    duplicate_numbers = _compute_duplicate_numbers(plan)
    if duplicate_numbers:
        logger.warning(
            'EPUB merge produced %d duplicated chapter number(s) in "%s": %s',
            len(duplicate_numbers),
            plan.title,
            ", ".join(duplicate_numbers),
        )

    # Build chapter_map: one entry per merged chapter, excluding non-chapter documents
    chapter_map = tuple(
        MergedChapter(c.book_index, c.source_href, c.filename)
        for c in plan.chapters
        if c.item_id.lower() not in NON_CHAPTER_ITEM_IDS
    )
    logger.debug(
        "Merge chapter map: %d %s for %d chapter(s)",
        len(chapter_map),
        "entry" if len(chapter_map) == 1 else "entries",
        total,
    )

    return MergeOutcome(
        chapter_count=total,
        title=renamed,
        contributions=contributions_tuple,
        duplicate_numbers=duplicate_numbers,
        chapter_map=chapter_map,
    )


def _write_and_verify(plan: MergePlan, target: Path, started: float) -> int:
    """Write merge plan to a staged file, verify it, and atomically replace target.

    Renders the plan to a temporary sibling file, reopens it to verify
    structural integrity and count chapters, then replaces the target. If
    verification fails, the staged file is moved to a temporary directory
    for debugging.

    Args:
        plan: The merge plan to render and verify.
        target: Path to the target EPUB file.
        started: Monotonic time when the merge started (for logging).

    Returns:
        The total content-chapter count of the merged result.

    Raises:
        MergeStructureError: If the merged output fails to reopen or verify.
            The rejected output is preserved in a temp directory.
    """
    # Stage to a sibling file
    staged = target.with_name(target.name + ".merge-tmp")

    # Render and write to staged file
    render_plan(plan).save(staged)

    try:
        # Reopen and verify
        doc = EpubDocument.open(staged)
        total = doc.content_chapter_count()
    except EpubError as exc:
        # Verification failed: preserve the rejected output
        _preserve(staged)
        raise MergeStructureError(f"merged output failed verification: {exc}") from exc

    # Success: atomically replace target
    staged.replace(target)

    elapsed = time.monotonic() - started
    logger.info(
        'EPUB merge finished: "%s" -> %d chapter(s), %d file(s), version=%s in %.1fs',
        plan.title,
        total,
        len(plan.files),
        plan.version,
        elapsed,
    )
    return total


def _preserve(staged: Path) -> None:
    """Move a rejected output file to a temporary directory for debugging.

    Args:
        staged: Path to the rejected EPUB file.
    """
    artifact_name = f"ebookerr-merge-failed-{uuid.uuid4().hex[:8]}.epub"
    artifact_path = Path(tempfile.gettempdir()) / artifact_name
    shutil.move(str(staged), str(artifact_path))
    logger.error(
        "EPUB merge failed for target=%s: rejected output kept at %s",
        staged.name,
        artifact_path,
    )
