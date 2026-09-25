"""Tests for the NormalizeCounts accumulator."""

from dataclasses import fields

import pytest
from epub_normalize.normalize.counts import NormalizeCounts


class TestDefaults:
    """Test NormalizeCounts default initialization."""

    def test_defaults_are_all_zero(self) -> None:
        """Every field of NormalizeCounts() equals 0."""
        assert all(getattr(NormalizeCounts(), f.name) == 0 for f in fields(NormalizeCounts))


class TestAdd:
    """Test NormalizeCounts.add() method."""

    def test_add_sums_every_field(self) -> None:
        """add() sums every field of the other accumulator into self."""
        a = NormalizeCounts(
            declarations_removed=2,
            rules_removed=1,
            font_files_removed=3,
            files_skipped_marker=4,
        )
        b = NormalizeCounts(
            declarations_removed=5,
            declarations_converted=7,
            files_skipped_marker=1,
        )
        a.add(b)

        assert a.declarations_removed == 7
        assert a.declarations_converted == 7
        assert a.rules_removed == 1
        assert a.font_files_removed == 3
        assert a.files_skipped_marker == 5
        assert a.style_attributes_removed == 0

    def test_add_leaves_the_other_untouched(self) -> None:
        """add() does not modify the other accumulator."""
        a = NormalizeCounts(
            declarations_removed=2,
            rules_removed=1,
            font_files_removed=3,
            files_skipped_marker=4,
        )
        b = NormalizeCounts(
            declarations_removed=5,
            declarations_converted=7,
            files_skipped_marker=1,
        )
        a.add(b)

        assert b.declarations_removed == 5
        assert b.files_skipped_marker == 1

    def test_add_covers_every_declared_field(self) -> None:
        """add() handles all declared fields."""
        x = NormalizeCounts(**{f.name: 1 for f in fields(NormalizeCounts)})
        y = NormalizeCounts()
        y.add(x)

        assert all(getattr(y, f.name) == 1 for f in fields(NormalizeCounts))


class TestTouched:
    """Test NormalizeCounts.touched property."""

    def test_touched_is_false_when_nothing_changed(self) -> None:
        """touched is False when all counters are zero."""
        assert NormalizeCounts().touched is False

    def test_touched_ignores_skipped_files(self) -> None:
        """touched ignores files_skipped_marker counter."""
        assert NormalizeCounts(files_skipped_marker=9).touched is False

    @pytest.mark.parametrize(
        "field_name",
        [
            "declarations_removed",
            "declarations_converted",
            "rules_removed",
            "style_attributes_removed",
            "viewport_metas_removed",
            "css_files_changed",
            "xhtml_files_changed",
            "font_files_removed",
        ],
    )
    def test_touched_is_true_for_each_change_counter(self, field_name: str) -> None:
        """touched is True when any change counter is non-zero."""
        assert NormalizeCounts(**{field_name: 1}).touched is True
