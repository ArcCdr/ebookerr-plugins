"""Merging read positions when several books become one (``RP-D7``, ``RP-D16``).

A merge concatenates its inputs' chapters, so every input's reading progress still refers to
a real chapter of the survivor — at a shifted identity. The most advanced position wins: a
reader who reached chapter 2 of the second input has, in the merged book, reached the chapter
that input's chapter 2 became, whatever they did or did not read before it. That is
deliberate — pulling earlier chapters in to complete a book is the common reason to merge,
and rewinding the reader to the first unread chapter would be wrong.

Resolution proceeds per position by identity facets (key, document href) before falling back
to count arithmetic (``RP-D16``). This avoids the false remapping when chapters are renumbered
or reordered across the merge.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The repository keeps only this many history entries per book (READ_POSITION_CAP), so the
# merge emits at most this many reconstructed entries: the highest-numbered chapters, which
# are the ones a reader might want to return to. It must equal ``READ_POSITION_CAP``
# (pinned by the plugin's ``test_merge_read_positions.py``; the core's own cap is pinned by
# ``tests/unit/test_plugin_apply.py::test_rotation_via_apply_path``, and the two must stay
# equal).
MERGED_HISTORY_LIMIT = 8


@dataclass(frozen=True, slots=True)
class SourcePosition:
    """One input book's stored reading progress, as it stood before the merge.

    Attributes:
        input_index: Which input this came from — ``0`` is the survivor, matching the order
            of ``MergeOutcome.contributions``.
        chapter_index: The 1-based content-chapter index within *that input*; ``0`` means
            front matter (nothing read yet).
        chapter_progress: Fraction read of that chapter, ``0.0``–``1.0``.
        completed: Whether that input was flagged fully read.
        captured_at: The original capture instant, carried through verbatim so a
            reconstructed entry keeps its real timestamp.
        chapter_href: The XHTML basename of the chapter (``RP-D19``), or ``None`` if
            unknown.
        chapter_key: The rebuild-stable identity key of the chapter (``RP-D19``), or
            ``None`` if unknown.
    """

    input_index: int
    chapter_index: int
    chapter_progress: float
    completed: bool
    captured_at: str
    chapter_href: str | None = None
    chapter_key: str | None = None


@dataclass(frozen=True, slots=True)
class MergedPosition:
    """One reconstructed history entry for the merged book.

    Attributes:
        chapter_index: The 1-based content-chapter index in the merged book.
        chapter_progress: Fraction read of that chapter, ``0.0``–``1.0``.
        completed: True only for the entry that marks the whole merged book read.
        captured_at: The originating entry's capture instant, preserved.
        chapter_key: The rebuild-stable identity key of the merged chapter (``RP-D19``).
        chapter_title: The title of the merged chapter, or ``None``.
        chapter_number: The all-digit chapter number from the merged chapter's title, or
            ``None``.
        chapter_href: The XHTML basename of the merged chapter.
        inferred: Whether the chapter was placed by arithmetic fallback rather than
            identity.
    """

    chapter_index: int
    chapter_progress: float
    completed: bool
    captured_at: str
    chapter_key: str | None
    chapter_title: str | None
    chapter_number: int | None
    chapter_href: str | None
    inferred: bool = False


def merge_read_positions(  # noqa: C901
    positions: Sequence[SourcePosition],
    contributions: tuple[int, ...],
    chapter_map: Sequence[Any],
    merged: Sequence[Any],
) -> list[MergedPosition]:
    """Remap every input's reading progress onto the merged book's chapter identity.

    Resolution per position proceeds by identity facets (key, document href) before
    falling back to count arithmetic (``RP-D16``). This avoids false remapping when
    chapters are renumbered or reordered across the merge.

    Precedence order:
    1. Completed input → the last merged chapter of that input's block at 1.0 progress.
    2. By document: uses ``chapter_map`` to look up the merged chapter corresponding to
       the source document basename.
    3. By key: ``pos.chapter_key`` matches a merged chapter's ``key``.
    4. Arithmetic: ``offset + chapter_index`` where offset is the sum of preceding
       contributions; marked ``inferred=True`` and logs a WARNING.
    5. None: position is dropped with a WARNING when no facet resolves it.

    A non-chapter position (``chapter_index == 0`` and not ``completed``) is silently skipped.

    Where two inputs land on the same merged chapter, the **highest** progress wins, so
    nothing a reader achieved is lost. At most :data:`MERGED_HISTORY_LIMIT` entries are
    returned — the highest-numbered chapters — because the repository prunes to that many
    anyway; returning more would silently drop the oldest.

    ``completed`` is set on the returned newest entry **only** when it sits on the merged
    book's final chapter at full progress: a merged book is read only when its last chapter
    is.

    Args:
        positions: Every input's stored position; inputs with nothing stored are simply absent.
        contributions: Per-input content-chapter counts in input order
            (``MergeOutcome.contributions``).
        chapter_map: Mapping from merged chapters to their provenance (input, source document).
        merged: The merged book's chapter table, with ``ordinal``, ``key``, ``title``,
            ``number``, ``href`` attributes.

    Returns:
        Reconstructed entries ordered oldest chapter first, at most
        :data:`MERGED_HISTORY_LIMIT` of them; empty when ``positions`` is empty or
        ``contributions`` is empty. Each entry carries the merged chapter's identity
        facets and an ``inferred`` flag.
    """
    if not positions or not contributions:
        return []

    # Build a lookup: (input_index, source_filename_basename) -> merged_ordinal
    doc_lookup: dict[tuple[int, str], int] = {}
    for i, mc in enumerate(chapter_map):
        # basename of the OPF-relative source_href
        source_basename = Path(mc.source_href).name
        doc_lookup[(mc.input_index, source_basename)] = i

    # Build a lookup: chapter_key -> merged_ordinal (0-based array position, matching doc_lookup —
    # not merged_ch.ordinal, which is the chapter's 1-based ordinal, not its position in `merged`)
    key_lookup: dict[str, int] = {}
    for i, merged_ch in enumerate(merged):
        if merged_ch.key:
            key_lookup[merged_ch.key] = i

    # Collect resolved positions by merged ordinal
    merged_by_ordinal: dict[
        int, tuple[float, str, bool]
    ] = {}  # ordinal -> (progress, captured_at, inferred)

    for pos in positions:
        # Skip if input_index is out of range
        if pos.input_index >= len(contributions):
            continue

        # Skip non-chapter positions (chapter_index == 0, not completed)
        if pos.chapter_index == 0 and not pos.completed:
            continue

        # Resolve the merged ordinal
        merged_ordinal: int | None = None
        inferred = False

        # 1. Completed input → last chapter of its block
        if pos.completed:
            offset = sum(contributions[: pos.input_index])
            merged_ordinal = offset + contributions[pos.input_index] - 1
            progress = 1.0
        else:
            progress = pos.chapter_progress

            # 2. By document: lookup using source_href basename
            if pos.chapter_href and (pos.input_index, pos.chapter_href) in doc_lookup:
                merged_ordinal = doc_lookup[(pos.input_index, pos.chapter_href)]

            # 3. By key: if no document match, try key
            if merged_ordinal is None and pos.chapter_key and pos.chapter_key in key_lookup:
                merged_ordinal = key_lookup[pos.chapter_key]

            # 4. Arithmetic fallback
            if merged_ordinal is None:
                offset = sum(contributions[: pos.input_index])
                candidate = offset + pos.chapter_index - 1  # Convert to 0-based ordinal
                if 0 <= candidate < len(merged):
                    merged_ordinal = candidate
                    inferred = True
                    logger.warning(
                        "Merge placed a read position by chapter count, not by identity: "
                        "input %d chapter_index %d -> merged chapter %d",
                        pos.input_index,
                        pos.chapter_index,
                        candidate,
                    )

            # 5. None: drop with warning
            if merged_ordinal is None:
                logger.warning(
                    "Merge dropped a read position it could not place: input %d chapter_index %d",
                    pos.input_index,
                    pos.chapter_index,
                )
                continue

        # Update the best progress for this ordinal; on tie, keep latest captured_at
        if merged_ordinal not in merged_by_ordinal:
            merged_by_ordinal[merged_ordinal] = (progress, pos.captured_at, inferred)
        else:
            existing_progress, existing_captured_at, existing_inferred = merged_by_ordinal[
                merged_ordinal
            ]
            if progress > existing_progress or (
                progress == existing_progress and pos.captured_at > existing_captured_at
            ):
                # Preserve the inferred flag: if either entry is inferred, mark as inferred
                merged_inferred = existing_inferred or inferred
                merged_by_ordinal[merged_ordinal] = (progress, pos.captured_at, merged_inferred)

    # Sort by ordinal
    sorted_ordinals = sorted(merged_by_ordinal.items())

    # Keep only the last MERGED_HISTORY_LIMIT entries
    if len(sorted_ordinals) > MERGED_HISTORY_LIMIT:
        sorted_ordinals = sorted_ordinals[-MERGED_HISTORY_LIMIT:]

    # Build final result with completed flags and merged chapter metadata
    total_chapters = len(merged)
    result: list[MergedPosition] = []

    for i, (ordinal, (progress, captured_at, inferred)) in enumerate(sorted_ordinals):
        is_last = i == len(sorted_ordinals) - 1
        is_completed = is_last and ordinal == (total_chapters - 1) and progress >= 1.0

        # Fetch the merged chapter metadata
        merged_ch = merged[ordinal]

        # Extract chapter_number from the number token (all-digit suffix)
        from ebookerr_sdk.readpos import chapter_number_facet

        chapter_number = chapter_number_facet(merged_ch.number) if merged_ch.number else None

        result.append(
            MergedPosition(
                # +1: `ordinal` here is the 0-based position in `merged`; chapter_index is the
                # 1-based semantic index every ReadPosition consumer expects (RP-SEM-1).
                chapter_index=ordinal + 1,
                chapter_progress=progress,
                completed=is_completed,
                captured_at=captured_at,
                chapter_key=merged_ch.key or None,
                chapter_title=merged_ch.title or None,
                chapter_number=chapter_number,
                chapter_href=merged_ch.href or None,
                inferred=inferred,
            )
        )

    return result
