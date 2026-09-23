"""Tests for merged read-position identity mapping (RP-D7, RP-D16)."""

import logging
from types import SimpleNamespace

from epub_merge.merge.read_positions import (
    MERGED_HISTORY_LIMIT,
    SourcePosition,
    merge_read_positions,
)


class TestMergeReadPositions:
    """Test suite for merge_read_positions function."""

    def test_empty_positions_returns_empty(self) -> None:
        """Empty positions list returns empty result."""
        result = merge_read_positions([], (1, 1), [], [])
        assert result == []

    def test_empty_contributions_returns_empty(self) -> None:
        """Empty contributions tuple returns empty result."""
        pos = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.4,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
        )
        result = merge_read_positions([pos], (), [], [])
        assert result == []

    def test_single_source_unshifted(self) -> None:
        """Single source at input 0 is placed correctly."""
        pos = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.4,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c1.xhtml",
        )
        # merged chapter at ordinal 0 with key "k1"
        merged = [SimpleNamespace(ordinal=0, key="k1", title="Ch 1", number="1", href="c1.xhtml")]
        chapter_map = []
        result = merge_read_positions([pos], (1,), chapter_map, merged)
        assert len(result) == 1
        assert result[0].chapter_index == 1
        assert result[0].chapter_progress == 0.4
        assert result[0].completed is False

    def test_second_input_is_placed_by_document(self, caplog) -> None:
        """Position at input 1 placed by document identity lookup."""
        # Survivor: 2 chapters (c1.xhtml, c2.xhtml)
        # Input 1: 1 chapter (file0001.xhtml)
        # Merged: c1.xhtml (input 0), c2.xhtml (input 0), c3.xhtml (input 1)
        pos = SourcePosition(
            input_index=1,
            chapter_index=1,
            chapter_progress=0.6,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="file0001.xhtml",
        )
        chapter_map = [
            SimpleNamespace(input_index=0, source_href="OEBPS/c1.xhtml", filename="c1.xhtml"),
            SimpleNamespace(input_index=0, source_href="OEBPS/c2.xhtml", filename="c2.xhtml"),
            SimpleNamespace(input_index=1, source_href="OEBPS/file0001.xhtml", filename="c3.xhtml"),
        ]
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="Ch 1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="Ch 2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="k3", title="Ch 3", number="1", href="c3.xhtml"),
        ]
        result = merge_read_positions([pos], (2, 1), chapter_map, merged)
        assert len(result) == 1
        assert result[0].chapter_index == 3  # ordinal 2 (0-based) + 1
        assert result[0].chapter_progress == 0.6
        assert result[0].inferred is False

    def test_a_front_matter_input_is_placed_by_document_not_by_count(self) -> None:
        """Front-matter input placed by document, not by count arithmetic."""
        # Input 1: Prologue (index 0) + Ch 1 (index 1) at file0002.xhtml
        # contributions: (3, 2) -> input 0 has 3 chapters, input 1 has 2
        # If placed by count: 3 + 1 = 4
        # If placed by document: should find file0002.xhtml which is at merged ordinal 4
        pos = SourcePosition(
            input_index=1,
            chapter_index=1,  # First content chapter of input 1
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="file0002.xhtml",
        )
        chapter_map = [
            SimpleNamespace(input_index=0, source_href="OEBPS/c1.xhtml", filename="c1.xhtml"),
            SimpleNamespace(input_index=0, source_href="OEBPS/c2.xhtml", filename="c2.xhtml"),
            SimpleNamespace(input_index=0, source_href="OEBPS/c3.xhtml", filename="c3.xhtml"),
            SimpleNamespace(input_index=1, source_href="OEBPS/prologue.xhtml", filename="c4.xhtml"),
            SimpleNamespace(input_index=1, source_href="OEBPS/file0002.xhtml", filename="c5.xhtml"),
        ]
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="k3", title="C3", number="3", href="c3.xhtml"),
            SimpleNamespace(ordinal=3, key="k4", title="Prologue", number=None, href="c4.xhtml"),
            SimpleNamespace(ordinal=4, key="k5", title="Ch 1", number="1", href="c5.xhtml"),
        ]
        result = merge_read_positions([pos], (3, 2), chapter_map, merged)
        assert len(result) == 1
        assert (
            result[0].chapter_index == 5
        )  # Not 4 by count coincidentally (by-count also lands on 4), but verified by document
        assert result[0].inferred is False

    def test_placement_by_key_when_the_document_is_unknown(self) -> None:
        """Position placed by chapter_key when href is unknown."""
        pos = SourcePosition(
            input_index=0,
            chapter_index=2,
            chapter_progress=0.75,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href=None,  # Unknown
            chapter_key="key-4",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="key-1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="key-2", title="C2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="key-3", title="C3", number="3", href="c3.xhtml"),
            SimpleNamespace(ordinal=3, key="key-4", title="C4", number="4", href="c4.xhtml"),
        ]
        result = merge_read_positions([pos], (4,), [], merged)
        assert len(result) == 1
        assert result[0].chapter_index == 4  # ordinal 3 (0-based) + 1
        assert result[0].chapter_key == "key-4"
        assert result[0].inferred is False

    def test_arithmetic_fallback_is_flagged(self, caplog) -> None:
        """Arithmetic fallback is marked inferred and logs WARNING."""
        pos = SourcePosition(
            input_index=0,
            chapter_index=2,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href=None,
            chapter_key=None,
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
        ]
        with caplog.at_level(logging.WARNING):
            result = merge_read_positions([pos], (2,), [], merged)
        assert len(result) == 1
        assert result[0].chapter_index == 2  # ordinal 1 (0-based, 2nd chapter) + 1
        assert result[0].inferred is True
        assert any(
            "Merge placed a read position by chapter count" in record.message
            for record in caplog.records
        )

    def test_unplaceable_position_is_dropped_with_a_warning(self, caplog) -> None:
        """Position that cannot be placed is dropped with WARNING."""
        pos = SourcePosition(
            input_index=0,
            chapter_index=99,  # Out of range
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="unknown.xhtml",
            chapter_key="unknown-key",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
        ]
        with caplog.at_level(logging.WARNING):
            result = merge_read_positions([pos], (1,), [], merged)
        assert len(result) == 0
        assert any(
            "Merge dropped a read position it could not place" in record.message
            for record in caplog.records
        )

    def test_completed_input_lands_on_its_block_end(self) -> None:
        """Completed input lands on last chapter of its contribution block."""
        pos = SourcePosition(
            input_index=0,
            chapter_index=0,  # Doesn't matter for completed
            chapter_progress=0.0,
            completed=True,
            captured_at="2026-01-01T00:00:00Z",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="k3", title="C3", number="1", href="c3.xhtml"),
            SimpleNamespace(ordinal=3, key="k4", title="C4", number="2", href="c4.xhtml"),
            SimpleNamespace(ordinal=4, key="k5", title="C5", number="3", href="c5.xhtml"),
        ]
        # Input 0 contributes 2 chapters, input 1 contributes 3
        # So input 0's last chapter is at merged index 1, input 1's last is at 4
        result = merge_read_positions([pos], (2, 3), [], merged)
        assert len(result) == 1
        assert result[0].chapter_index == 2  # Last of input 0's block (ordinal 1, 0-based, + 1)
        assert result[0].chapter_progress == 1.0
        assert result[0].inferred is False

    def test_highest_progress_wins_on_the_same_chapter(self) -> None:
        """Two positions on same merged chapter keep the highest progress."""
        pos1 = SourcePosition(
            input_index=0,
            chapter_index=2,
            chapter_progress=0.3,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c2.xhtml",
        )
        pos2 = SourcePosition(
            input_index=0,
            chapter_index=2,
            chapter_progress=0.8,
            completed=False,
            captured_at="2026-06-01T00:00:00Z",
            chapter_href="c2.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="k3", title="C3", number="3", href="c3.xhtml"),
        ]
        result = merge_read_positions([pos1, pos2], (3,), [], merged)
        assert len(result) == 1
        assert result[0].chapter_index == 2
        assert result[0].chapter_progress == 0.8

    def test_tie_keeps_the_later_capture(self) -> None:
        """On tied progress, keep the later captured_at timestamp."""
        pos1 = SourcePosition(
            input_index=0,
            chapter_index=2,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c2.xhtml",
        )
        pos2 = SourcePosition(
            input_index=0,
            chapter_index=2,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-06-01T00:00:00Z",
            chapter_href="c2.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="k3", title="C3", number="3", href="c3.xhtml"),
        ]
        result = merge_read_positions([pos1, pos2], (3,), [], merged)
        assert len(result) == 1
        assert result[0].captured_at == "2026-06-01T00:00:00Z"

    def test_completed_only_when_last_chapter_full(self) -> None:
        """completed=True only on final entry when it's at last chapter with full progress."""
        pos1 = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c1.xhtml",
        )
        pos2 = SourcePosition(
            input_index=1,
            chapter_index=1,
            chapter_progress=1.0,
            completed=False,
            captured_at="2026-06-01T00:00:00Z",
            chapter_href="c2.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
        ]
        result = merge_read_positions([pos1, pos2], (1, 1), [], merged)
        assert len(result) == 2
        assert result[0].completed is False
        assert result[1].completed is True
        assert result[1].chapter_index == 2

    def test_not_completed_when_last_chapter_partial(self) -> None:
        """completed=False when final entry is at last chapter but not 100% progress."""
        pos1 = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c1.xhtml",
        )
        pos2 = SourcePosition(
            input_index=1,
            chapter_index=1,
            chapter_progress=0.9,
            completed=False,
            captured_at="2026-06-01T00:00:00Z",
            chapter_href="c2.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
        ]
        result = merge_read_positions([pos1, pos2], (1, 1), [], merged)
        assert len(result) == 2
        assert result[0].completed is False
        assert result[1].completed is False

    def test_not_completed_when_final_chapter_unreached(self) -> None:
        """completed=False when the most advanced entry is not at the final chapter."""
        pos = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c1.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
        ]
        result = merge_read_positions([pos], (2,), [], merged)
        assert len(result) == 1
        assert result[0].completed is False

    def test_history_capped_at_eight(self) -> None:
        """History is capped at MERGED_HISTORY_LIMIT (8) entries."""
        positions = [
            SourcePosition(
                input_index=i,
                chapter_index=1,
                chapter_progress=0.5,
                completed=False,
                captured_at=f"2026-01-{i + 1:02d}T00:00:00Z",
                chapter_href=f"c{i + 1}.xhtml",
            )
            for i in range(10)
        ]
        merged = [
            SimpleNamespace(
                ordinal=i,
                key=f"k{i + 1}",
                title=f"C{i + 1}",
                number=str(i + 1),
                href=f"c{i + 1}.xhtml",
            )
            for i in range(10)
        ]
        result = merge_read_positions(positions, (1,) * 10, [], merged)
        assert len(result) == MERGED_HISTORY_LIMIT
        assert result[0].chapter_index == 3
        assert result[-1].chapter_index == 10

    def test_history_ordered_oldest_chapter_first(self) -> None:
        """Returned entries are ordered by chapter_index ascending."""
        pos1 = SourcePosition(
            input_index=0,
            chapter_index=2,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c2.xhtml",
        )
        pos2 = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-02T00:00:00Z",
            chapter_href="c1.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="k3", title="C3", number="3", href="c3.xhtml"),
        ]
        result = merge_read_positions([pos1, pos2], (3,), [], merged)
        assert result[0].chapter_index == 1
        assert result[1].chapter_index == 2

    def test_out_of_range_input_index_skipped(self) -> None:
        """Position with out-of-range input_index is silently skipped."""
        pos1 = SourcePosition(
            input_index=9,
            chapter_index=1,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-01T00:00:00Z",
            chapter_href="c1.xhtml",
        )
        pos2 = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.5,
            completed=False,
            captured_at="2026-01-02T00:00:00Z",
            chapter_href="c1.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
        ]
        result = merge_read_positions([pos1, pos2], (1, 1), [], merged)
        assert len(result) == 1
        assert result[0].chapter_index == 1

    def test_captured_at_preserved_verbatim(self) -> None:
        """captured_at timestamp is preserved exactly."""
        timestamp = "2026-05-15T14:30:45.123456Z"
        pos = SourcePosition(
            input_index=0,
            chapter_index=1,
            chapter_progress=0.5,
            completed=False,
            captured_at=timestamp,
            chapter_href="c1.xhtml",
        )
        merged = [
            SimpleNamespace(ordinal=0, key="k1", title="C1", number="1", href="c1.xhtml"),
            SimpleNamespace(ordinal=1, key="k2", title="C2", number="2", href="c2.xhtml"),
            SimpleNamespace(ordinal=2, key="k3", title="C3", number="3", href="c3.xhtml"),
        ]
        result = merge_read_positions([pos], (3,), [], merged)
        assert result[0].captured_at == timestamp

    def test_the_merge_keeps_as_many_positions_as_the_repository(self) -> None:
        """MERGED_HISTORY_LIMIT is 8.

        The core's own ``READ_POSITION_CAP`` is pinned equal to it by
        ``tests/unit/test_plugin_apply.py::test_rotation_via_apply_path``.
        """
        assert MERGED_HISTORY_LIMIT == 8
