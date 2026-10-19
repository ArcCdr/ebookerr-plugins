"""HTML presentational attributes that a styling level removes from start tags (FR-NORM-20).

Presentational attributes (``bgcolor``, ``color``, ``face``, ``align``, ``width``,
``leftmargin`` and the like) override the reader's own styling in the same way the CSS
properties they mimic do, so each level's rule groups decide which of them go. The rules
are a table: each row names the group flag that enables it, the attributes it removes,
the tags it applies to (all tags when unset) and, where relevant, the value that triggers
the removal. Attribute values are compared after dropping surrounding whitespace and
quotes and lower-casing them. Elements that carry images or embedded content keep every
attribute, because an ``align`` on an image is a float rather than a text alignment.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

from epub_normalize.normalize.options import NormalizeOptions

_FONT_TAGS = frozenset({"font", "basefont"})
_CELL_TAGS = frozenset({"table", "td", "th", "tr"})
_TEXT_ALIGN_TAGS = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "td",
        "th",
        "tr",
        "li",
        "caption",
        "thead",
        "tbody",
        "tfoot",
        "col",
        "colgroup",
    }
)
_DIMENSION_TAGS = frozenset(
    {
        "table",
        "td",
        "th",
        "tr",
        "col",
        "colgroup",
        "caption",
        "thead",
        "tbody",
        "tfoot",
    }
)
_BODY_TAGS = frozenset({"body"})

_BODY_COLOR_ATTRS = frozenset({"text", "link", "vlink", "alink", "background"})
_CELL_COLOR_ATTRS = frozenset({"background", "bordercolor"})
_BODY_MARGIN_ATTRS = frozenset(
    {"leftmargin", "topmargin", "rightmargin", "bottommargin", "marginwidth", "marginheight"}
)
_ALIGN_VALUES = frozenset({"justify", "left"})
_FIXED_LENGTH = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:px)?\s*$")


def _is_alignment_keyword(value: str | None) -> bool:
    """Return whether *value* is an ``align`` keyword the level removes.

    Args:
        value: The normalized attribute value, or None for a bare attribute.

    Returns:
        True for ``justify`` and ``left``, False otherwise.
    """
    return value in _ALIGN_VALUES


def _is_fixed_length(value: str | None) -> bool:
    """Return whether *value* is a pixel length, not a percentage.

    Args:
        value: The normalized attribute value, or None for a bare attribute.

    Returns:
        True when *value* is a number with an optional ``px`` unit.
    """
    return value is not None and _FIXED_LENGTH.match(value) is not None


class _Rule(NamedTuple):
    """One row of the presentational-attribute table.

    Attributes:
        flag: The NormalizeOptions group flag that enables the row.
        attrs: The attribute names the row removes (lower-case).
        tags: The tags the row applies to, or None for every tag.
        condition: The test on the normalized value, or None to remove unconditionally.
    """

    flag: str
    attrs: frozenset[str]
    tags: frozenset[str] | None
    condition: Callable[[str | None], bool] | None = None


_RULES: tuple[_Rule, ...] = (
    _Rule("strip_colors", frozenset({"bgcolor"}), None),
    _Rule("strip_colors", frozenset({"color"}), _FONT_TAGS),
    _Rule("strip_colors", _BODY_COLOR_ATTRS, _BODY_TAGS),
    _Rule("strip_colors", _CELL_COLOR_ATTRS, _CELL_TAGS),
    _Rule("strip_fonts", frozenset({"face"}), _FONT_TAGS),
    _Rule("strip_text_alignment", frozenset({"align"}), _TEXT_ALIGN_TAGS, _is_alignment_keyword),
    _Rule(
        "strip_fixed_dimensions",
        frozenset({"width", "height"}),
        _DIMENSION_TAGS,
        _is_fixed_length,
    ),
    _Rule("strip_page_layout", _BODY_MARGIN_ATTRS, _BODY_TAGS),
    _Rule("headings_only_sizes", frozenset({"size"}), _FONT_TAGS),
)


def _normalize_value(value: str) -> str:
    """Return *value* without surrounding whitespace or quotes, lower-cased.

    Args:
        value: The raw attribute value as it appears in the start tag.

    Returns:
        The value a rule's condition is tested against.
    """
    return value.strip().strip("\"'").strip().lower()


def _is_removed(tag: str, name: str, value: str | None, options: NormalizeOptions) -> bool:
    """Return whether the attribute *name* of *tag* is removed under *options*.

    Args:
        tag: The lower-case tag name.
        name: The lower-case attribute name.
        value: The raw attribute value, or None for a bare attribute.
        options: The rule groups the level runs.

    Returns:
        True when some enabled rule matches the attribute.
    """
    normalized = None if value is None else _normalize_value(value)
    for rule in _RULES:
        if name not in rule.attrs or not getattr(options, rule.flag):
            continue
        if rule.tags is not None and tag not in rule.tags:
            continue
        if rule.condition is None or rule.condition(normalized):
            return True
    return False


def strip_presentational_attributes(
    tag: str, attrs: list[tuple[str, str | None]], *, options: NormalizeOptions
) -> list[tuple[str, str | None]]:
    """Return the start tag's attributes minus the presentational ones the level removes.

    Image-like and embedded elements (``img``, ``object``, ``svg``, ``video`` and similar)
    have no rule and keep every attribute. Order of the surviving attributes is kept.

    Args:
        tag: The lower-case tag name of the start tag.
        attrs: The start tag's attributes as ``(lower-case name, raw value or None)`` pairs.
        options: The rule groups the level runs.

    Returns:
        The surviving ``(name, raw value)`` pairs, in their original order.
    """
    return [(name, value) for name, value in attrs if not _is_removed(tag, name, value, options)]
