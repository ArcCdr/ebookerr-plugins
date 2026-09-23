"""Tests for EPUB normalizer options and ruleset signature.

Tests the NormalizeOptions dataclass defaults, immutability, and signature
computation via options_signature().
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from epub_normalize.normalize import (
    NormalizeOptions,
    options_signature,
)


def test_defaults_match_the_documented_matrix() -> None:
    """Verify all seven default flags match the documented matrix."""
    opts = NormalizeOptions()
    assert opts.strip_fonts is True
    assert opts.strip_colors is True
    assert opts.convert_font_sizes is True
    assert opts.strip_line_spacing is True
    assert opts.strip_fixed_dimensions is True
    assert opts.preserve_alignment is True
    assert opts.normalize_margins is False


def test_options_are_frozen() -> None:
    """Verify NormalizeOptions is frozen and immutable."""
    opts = NormalizeOptions()
    with pytest.raises(dataclasses.FrozenInstanceError):
        opts.strip_fonts = False  # type: ignore[misc]


def test_signature_is_twelve_lowercase_hex() -> None:
    """Verify options_signature returns exactly 12 lowercase hex characters."""
    sig = options_signature(NormalizeOptions())
    assert len(sig) == 12
    assert re.fullmatch(r"[0-9a-f]{12}", sig) is not None


def test_signature_is_stable_for_equal_options() -> None:
    """Verify the same options always produce the same signature."""
    sig1 = options_signature(NormalizeOptions())
    sig2 = options_signature(NormalizeOptions())
    assert sig1 == sig2


def test_signature_changes_when_any_flag_changes() -> None:
    """Verify flipping any single flag changes the signature.

    Also verify the seven signatures are pairwise distinct.
    """
    baseline = NormalizeOptions()
    baseline_sig = options_signature(baseline)

    flag_names = (
        "strip_fonts",
        "strip_colors",
        "convert_font_sizes",
        "strip_line_spacing",
        "strip_fixed_dimensions",
        "preserve_alignment",
        "normalize_margins",
    )

    modified_sigs = []
    for flag_name in flag_names:
        current_value = getattr(baseline, flag_name)
        modified_opts = dataclasses.replace(baseline, **{flag_name: not current_value})
        modified_sig = options_signature(modified_opts)

        # Each modified signature must differ from baseline
        assert modified_sig != baseline_sig, f"Signature did not change for {flag_name}"
        modified_sigs.append(modified_sig)

    # All seven modified signatures must be pairwise distinct
    assert len(set(modified_sigs)) == 7


def test_signature_depends_on_the_ruleset_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify signature changes when RULESET_VERSION is patched."""
    baseline_sig = options_signature(NormalizeOptions())

    # Patch RULESET_VERSION to a different value
    monkeypatch.setattr("src.services.epub_normalize.options.RULESET_VERSION", "2")

    # Re-compute signature with patched version
    new_sig = options_signature(NormalizeOptions())

    # Signatures must differ
    assert new_sig != baseline_sig


def test_package_reexports() -> None:
    """Verify package __init__ re-exports the three public symbols."""
    from epub_normalize.normalize import (
        RULESET_VERSION,
        NormalizeOptions,
        options_signature,
    )

    assert RULESET_VERSION == "1"
    # Verify they are callable/instantiable
    opts = NormalizeOptions()
    sig = options_signature(opts)
    assert isinstance(sig, str)
