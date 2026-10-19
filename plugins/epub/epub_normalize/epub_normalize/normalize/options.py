"""Styling levels and the rule groups each level runs (FR-NORM-15, TR-NORM-8).

The plugin's one setting names a level. options_for_level maps it to the NormalizeOptions
the engine runs, and options_signature hashes the rule groups (never the level's name) into
the per-file idempotency marker, so a level change or a ruleset bump invalidates every marker.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

RULESET_VERSION = "2"

LEVELS: tuple[str, ...] = ("light", "standard", "strict")
"""The three values of the plugin's one setting, lightest first (FR-NORM-15)."""

DEFAULT_LEVEL = "standard"

_FLAG_ORDER: tuple[str, ...] = (
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


@dataclass(frozen=True, slots=True)
class NormalizeOptions:
    """The rule groups one run applies; ``options_for_level`` builds it from the setting.

    The defaults are the ``standard`` level.
    """

    level: str = DEFAULT_LEVEL
    strip_colors: bool = True
    strip_fonts: bool = True
    convert_font_sizes: bool = True
    strip_line_spacing: bool = True
    strip_hyphenation: bool = True
    strip_text_alignment: bool = True
    strip_fixed_dimensions: bool = True
    strip_page_layout: bool = True
    normalize_margins: bool = True
    headings_only_sizes: bool = False
    plain_drop_caps: bool = False


_BY_LEVEL: dict[str, NormalizeOptions] = {
    "light": NormalizeOptions(
        level="light",
        convert_font_sizes=False,
        strip_line_spacing=False,
        strip_hyphenation=False,
        strip_text_alignment=False,
        strip_fixed_dimensions=False,
        strip_page_layout=False,
        normalize_margins=False,
    ),
    "standard": NormalizeOptions(),
    "strict": NormalizeOptions(level="strict", headings_only_sizes=True, plain_drop_caps=True),
}


def options_for_level(level: str) -> NormalizeOptions:
    """Return the rule groups of *level* (one of ``LEVELS``).

    Args:
        level: The level name, as stored in the plugin's setting.

    Returns:
        The NormalizeOptions that level runs.

    Raises:
        ValueError: *level* is not one of ``LEVELS``.
    """
    try:
        return _BY_LEVEL[level]
    except KeyError as exc:
        raise ValueError(
            f"unknown styling level {level!r}; expected one of {', '.join(LEVELS)}"
        ) from exc


def options_signature(options: NormalizeOptions) -> str:
    """Return the 12 hex characters identifying the ruleset version and the rule groups.

    The digest is SHA-256 over ``f"{RULESET_VERSION}|{flags}"``, flags being ``"1"``/``"0"``
    per name of ``_FLAG_ORDER``, in order (``level`` is not hashed: the flags are what the
    level means).

    Args:
        options: The normalization options to sign.

    Returns:
        The first 12 lowercase hex characters of the SHA256 hash of the ruleset version and the
        rule-group flags.
    """
    flags = "".join("1" if getattr(options, name) else "0" for name in _FLAG_ORDER)
    raw = f"{RULESET_VERSION}|{flags}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
