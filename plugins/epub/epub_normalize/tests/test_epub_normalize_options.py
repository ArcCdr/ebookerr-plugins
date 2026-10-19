"""Tests for the styling levels, their rule groups and the ruleset signature (FR-NORM-15).

Tests options_for_level(), the NormalizeOptions dataclass defaults and immutability, and the
signature computed by options_signature().
"""

from __future__ import annotations

import dataclasses
import re

import pytest
from epub_normalize.normalize import (
    NormalizeOptions,
    options_for_level,
    options_signature,
)

_GROUPS: tuple[str, ...] = (
    "strip_colors",
    "strip_fonts",
    "convert_font_sizes",
    "strip_line_spacing",
    "strip_hyphenation",
    "strip_text_alignment",
    "strip_fixed_dimensions",
    "strip_page_layout",
    "normalize_margins",
    "headings_only_sizes",
    "plain_drop_caps",
)

_TABLE: dict[str, tuple[bool, ...]] = {
    "light": (True, True, False, False, False, False, False, False, False, False, False),
    "standard": (True, True, True, True, True, True, True, True, True, False, False),
    "strict": (True, True, True, True, True, True, True, True, True, True, True),
}


def test_the_default_options_are_the_standard_level() -> None:
    assert NormalizeOptions() == options_for_level("standard")


@pytest.mark.parametrize("level", ["light", "standard", "strict"])
def test_each_level_sets_its_groups(level: str) -> None:
    options = options_for_level(level)
    assert options.level == level
    assert tuple(getattr(options, name) for name in _GROUPS) == _TABLE[level]


def test_an_unknown_level_is_refused() -> None:
    with pytest.raises(ValueError, match="loud"):
        options_for_level("loud")


def test_each_level_has_its_own_signature() -> None:
    signatures = [options_signature(options_for_level(level)) for level in _TABLE]
    assert len(set(signatures)) == 3
    for signature in signatures:
        assert re.fullmatch(r"[0-9a-f]{12}", signature) is not None


def test_the_level_name_is_not_hashed() -> None:
    assert options_signature(NormalizeOptions(level="light")) == options_signature(
        NormalizeOptions()
    )


def test_a_ruleset_bump_changes_the_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = options_signature(NormalizeOptions())
    monkeypatch.setattr("epub_normalize.normalize.options.RULESET_VERSION", "3")
    assert options_signature(NormalizeOptions()) != baseline


def test_options_are_frozen() -> None:
    options = NormalizeOptions()
    with pytest.raises(dataclasses.FrozenInstanceError):
        options.strip_fonts = False  # type: ignore[misc]
