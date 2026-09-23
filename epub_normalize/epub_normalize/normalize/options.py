"""Configuration options for EPUB normalization.

This module defines the NormalizeOptions dataclass, which is the single carrier
of every user-configurable normalization switch. The options_signature() function
computes a content-addressed hash of the options and ruleset version, enabling
per-file idempotency markers to invalidate themselves when the ruleset or the
user's settings change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

RULESET_VERSION = "1"

_FLAG_ORDER: tuple[str, ...] = (
    "strip_fonts",
    "strip_colors",
    "convert_font_sizes",
    "strip_line_spacing",
    "strip_fixed_dimensions",
    "preserve_alignment",
    "normalize_margins",
)


@dataclass(frozen=True, slots=True)
class NormalizeOptions:
    """User-configurable normalization switches.

    Attributes:
        strip_fonts: Remove font declarations. Defaults to True.
        strip_colors: Remove color declarations. Defaults to True.
        convert_font_sizes: Convert font sizes to relative units. Defaults to True.
        strip_line_spacing: Remove line-spacing declarations. Defaults to True.
        strip_fixed_dimensions: Remove fixed width/height. Defaults to True.
        preserve_alignment: Preserve text alignment. Defaults to True.
        normalize_margins: Normalize margin declarations. Defaults to False.
    """

    strip_fonts: bool = True
    strip_colors: bool = True
    convert_font_sizes: bool = True
    strip_line_spacing: bool = True
    strip_fixed_dimensions: bool = True
    preserve_alignment: bool = True
    normalize_margins: bool = False


def options_signature(options: NormalizeOptions) -> str:
    """Compute a content-addressed signature of options and ruleset version.

    Args:
        options: The normalization options to sign.

    Returns:
        The first 12 lowercase hex characters of the SHA256 hash of the
        ruleset version and flag values concatenated.
    """
    flags = "".join("1" if getattr(options, name) else "0" for name in _FLAG_ORDER)
    raw = f"{RULESET_VERSION}|{flags}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
