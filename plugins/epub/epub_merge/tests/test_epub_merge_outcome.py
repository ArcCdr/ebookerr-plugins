"""Tests for MergeOutcome."""

from __future__ import annotations

import dataclasses

import pytest
from epub_merge.merge import MergeOutcome


def test_outcome_is_frozen() -> None:
    """Assigning outcome.chapter_count raises AttributeError or FrozenInstanceError."""
    outcome = MergeOutcome(chapter_count=3)

    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        outcome.chapter_count = 4  # type: ignore[misc]
