"""Unit conversion for EPUB normalization.

The CSS reference pixel is 96 DPI: one inch equals 96 px, so the absolute-length
units are fixed ratios (1 point = 1/72 inch, 1 pica = 12 points, etc.). This
module converts absolute lengths to em, a relative unit.

The base is 16 px because that is the CSS initial value of `font-size` and every
mainstream reading system's default. A conversion to `em` rather than `rem` is
used because `rem` support is unreliable in older ADE-based EPUB 2 readers, which
is exactly the population this feature serves.

The output is clamped to [0.4em, 4em] to stop a publisher's oversized heading (e.g.
48pt = 4em) from becoming unreadably large when the user picks a large base font
size. Zero values are never clamped: they always convert to "0" (unitless),
because zero needs no unit and clamping it upward would be wrong.

Negative values are supported (e.g. negative text-indent is real typography). They
clamp by magnitude and preserve their sign: -1px → -0.4em, -200px → -4em.
"""

from __future__ import annotations

BASE_FONT_PX = 16.0
EM_MIN = 0.4
EM_MAX = 4.0
PX_PER_UNIT: dict[str, float] = {
    "px": 1.0,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
    "in": 96.0,
    "cm": 96.0 / 2.54,
    "mm": 96.0 / 25.4,
    "q": 96.0 / 101.6,
}


def is_absolute_unit(unit: str) -> bool:
    """Check if a CSS unit is absolute (not relative).

    Args:
        unit: A CSS unit name (e.g., "px", "em", "%").

    Returns:
        True if the unit is in PX_PER_UNIT (an absolute unit), False otherwise.
    """
    return unit.lower() in PX_PER_UNIT


def to_em(value: float, unit: str) -> str | None:
    """Convert an absolute CSS length to em on a 16 px base.

    Args:
        value: The numeric length value.
        unit: The CSS unit (e.g., "px", "pt", "cm").

    Returns:
        A string like "1.5em" or "0" for absolute units; None if the unit is
        relative or unknown.
    """
    factor = PX_PER_UNIT.get(unit.lower())
    if factor is None:
        return None
    em = value * factor / BASE_FONT_PX
    if em == 0:
        return "0"
    magnitude = min(max(abs(em), EM_MIN), EM_MAX)
    em = magnitude if em > 0 else -magnitude
    text = f"{round(em, 3):.3f}".rstrip("0").rstrip(".")
    return f"{text}em"
