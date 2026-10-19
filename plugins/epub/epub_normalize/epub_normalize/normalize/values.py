"""Value-level helpers for EPUB normalization (FR-NORM-3, FR-NORM-4).

These functions read and rewrite the component values of a single declaration:

- ``expand_font_shorthand`` reads the longhands a ``font`` shorthand sets, so a
  font rule can be judged property by property (FR-NORM-3).
- ``family_names`` and ``is_monospace`` read a ``font-family`` list and tell
  whether any family is a monospace one (FR-NORM-3).
- ``strip_colors`` removes the colour components and gradients from a
  colour-bearing shorthand such as ``border`` or ``background`` (FR-NORM-4).
- ``has_absolute_length``, ``is_viewport_length`` and ``convert_lengths`` find
  and rewrite absolute lengths as em against a base font size.

The keyword sets and the monospace and gradient name sets are CSS vocabulary.
``rules.py`` is meant to own the family and gradient sets; they are kept here
until that module takes them over.
"""

from __future__ import annotations

from typing import Any

import tinycss2
from tinycss2.color4 import parse_color

from .units import VIEWPORT_UNITS, is_absolute_unit, to_em

FONT_STYLE_KEYWORDS = frozenset({"italic", "oblique"})
FONT_VARIANT_KEYWORDS = frozenset({"small-caps"})
FONT_WEIGHT_KEYWORDS = frozenset({"bold", "bolder", "lighter"})
FONT_STRETCH_KEYWORDS = frozenset(
    {
        "ultra-condensed",
        "extra-condensed",
        "condensed",
        "semi-condensed",
        "semi-expanded",
        "expanded",
        "extra-expanded",
        "ultra-expanded",
    }
)
FONT_SIZE_KEYWORDS = frozenset(
    {
        "xx-small",
        "x-small",
        "small",
        "medium",
        "large",
        "x-large",
        "xx-large",
        "xxx-large",
        "larger",
        "smaller",
    }
)
SYSTEM_FONT_KEYWORDS = frozenset(
    {
        "caption",
        "icon",
        "menu",
        "message-box",
        "small-caption",
        "status-bar",
    }
)
CSS_WIDE_KEYWORDS = frozenset({"inherit", "initial", "unset", "revert", "revert-layer"})

_GRADIENT_FUNCTIONS: frozenset[str] = frozenset(
    {
        "linear-gradient",
        "radial-gradient",
        "conic-gradient",
        "repeating-linear-gradient",
        "repeating-radial-gradient",
        "repeating-conic-gradient",
        "-webkit-linear-gradient",
        "-webkit-radial-gradient",
        "-moz-linear-gradient",
        "-webkit-gradient",
    }
)
_MONOSPACE_FAMILIES: frozenset[str] = frozenset(
    {
        "monospace",
        "ui-monospace",
        "courier",
        "courier new",
        "courier prime",
        "courier std",
        "consolas",
        "menlo",
        "monaco",
        "lucida console",
        "lucida sans typewriter",
        "andale mono",
        "dejavu sans mono",
        "liberation mono",
        "source code pro",
        "fira code",
        "fira mono",
        "inconsolata",
        "sf mono",
        "roboto mono",
        "ubuntu mono",
        "noto sans mono",
        "pt mono",
        "cousine",
    }
)


def _significant(value: list[Any]) -> list[Any]:
    """Drop the whitespace and comment tokens from a component-value list.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        The remaining tokens, in their original order.
    """
    return [node for node in value if node.type not in ("whitespace", "comment")]


def _is_literal(node: Any, text: str) -> bool:
    """Check whether a token is the literal punctuation *text*.

    Args:
        node: A tinycss2 token.
        text: The punctuation character to match, e.g. ",".

    Returns:
        True if the token is a literal equal to *text*.
    """
    return bool(node.type == "literal" and node.value == text)


def _split_commas(value: list[Any]) -> list[list[Any]]:
    """Split a component-value list on its top-level commas.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        The comma-separated items, each a list of its tokens.
    """
    items: list[list[Any]] = [[]]
    for node in value:
        if _is_literal(node, ","):
            items.append([])
        else:
            items[-1].append(node)
    return items


def family_names(value: list[Any]) -> list[str]:
    """The lower-case family names of a ``font-family`` value.

    Args:
        value: A tinycss2 component-value list of a ``font-family`` declaration.

    Returns:
        One name per comma-separated item: a quoted string's value, or the
        identifiers of an unquoted name joined by one space. Empty items are
        dropped.
    """
    names: list[str] = []
    for item in _split_commas(value):
        tokens = _significant(item)
        if tokens and tokens[0].type == "string":
            name = tokens[0].value
        else:
            name = " ".join(node.value for node in tokens if node.type == "ident")
        if name:
            names.append(name.lower())
    return names


def is_monospace(value: list[Any]) -> bool:
    """Check whether any family name of a ``font-family`` value is monospace.

    Args:
        value: A tinycss2 component-value list of a ``font-family`` declaration.

    Returns:
        True if one of the family names is in the monospace set.
    """
    return any(name in _MONOSPACE_FAMILIES for name in family_names(value))


def _read_font_prefix(tokens: list[Any]) -> tuple[str, str, str, int]:
    """Read the optional style, variant and weight keywords of a font shorthand.

    Args:
        tokens: The significant tokens of the shorthand, in order.

    Returns:
        A tuple of (style, variant, weight, index), where index is the position of
        the first token that is not a style, variant, weight or stretch keyword.
        Keywords not present are "normal".
    """
    style, variant, weight = "normal", "normal", "normal"
    index = 0
    while index < len(tokens):
        node = tokens[index]
        word = node.lower_value if node.type == "ident" else ""
        if word in FONT_STYLE_KEYWORDS:
            style = word
        elif word in FONT_VARIANT_KEYWORDS:
            variant = word
        elif word in FONT_WEIGHT_KEYWORDS:
            weight = word
        elif node.type == "number" and node.is_integer and 1 <= node.int_value <= 1000:
            weight = str(node.int_value)
        elif word != "normal" and word not in FONT_STRETCH_KEYWORDS:
            break
        index += 1
    return style, variant, weight, index


def _is_font_size(node: Any) -> bool:
    """Check whether a token can be the size of a font shorthand.

    Args:
        node: A tinycss2 token.

    Returns:
        True for a dimension, a percentage, the number 0, or a font-size keyword.
    """
    if node.type in ("dimension", "percentage"):
        return True
    if node.type == "number":
        return bool(node.value == 0)
    return node.type == "ident" and node.lower_value in FONT_SIZE_KEYWORDS


def expand_font_shorthand(value: list[Any]) -> list[tuple[str, str]] | None:
    """The longhands a ``font`` shorthand sets.

    Args:
        value: A tinycss2 component-value list of a ``font`` declaration.

    Returns:
        The (property, value) pairs of font-style, font-variant, font-weight,
        font-size and line-height, plus font-family set to "monospace" when the
        family list is monospace. None when the shorthand cannot be read: no
        value, a system or CSS-wide keyword, a missing size, or no family list.
    """
    tokens = _significant(value)
    if not tokens:
        return None
    if (
        len(tokens) == 1
        and tokens[0].type == "ident"
        and (tokens[0].lower_value in SYSTEM_FONT_KEYWORDS | CSS_WIDE_KEYWORDS)
    ):
        return None
    style, variant, weight, index = _read_font_prefix(tokens)
    if index >= len(tokens) or not _is_font_size(tokens[index]):
        return None
    size = tinycss2.serialize(tokens[index : index + 1])
    index += 1
    line_height = "normal"
    if index < len(tokens) and _is_literal(tokens[index], "/"):
        if index + 1 >= len(tokens):
            return None
        line_height = tinycss2.serialize(tokens[index + 1 : index + 2])
        index += 2
    family = tokens[index:]
    if not family:
        return None
    longhands = [
        ("font-style", style),
        ("font-variant", variant),
        ("font-weight", weight),
        ("font-size", size),
        ("line-height", line_height),
    ]
    if is_monospace(family):
        longhands.append(("font-family", "monospace"))
    return longhands


def _is_colour(node: Any) -> bool:
    """Check whether a token is a colour.

    Args:
        node: A tinycss2 token.

    Returns:
        True for a hash, identifier or function token that parses as a colour.
    """
    return node.type in ("hash", "ident", "function") and parse_color(node) is not None


def _is_gradient(node: Any) -> bool:
    """Check whether a token is a gradient function.

    Args:
        node: A tinycss2 token.

    Returns:
        True for a function token naming a gradient.
    """
    return node.type == "function" and node.lower_name in _GRADIENT_FUNCTIONS


def _is_image(node: Any) -> bool:
    """Check whether a token is a url() image.

    Args:
        node: A tinycss2 token.

    Returns:
        True for a url token or a function named url.
    """
    return bool(node.type == "url" or (node.type == "function" and node.lower_name == "url"))


def strip_colors(value: list[Any], *, drop_images: bool) -> list[Any] | None:
    """A colour-bearing shorthand's value without its colour components.

    Args:
        value: A tinycss2 component-value list of a shorthand such as ``border``
            or ``background``.
        drop_images: Whether url() images are removed too.

    Returns:
        The remaining value, re-parsed with a leading space, or None when nothing
        is left once the colours, gradients and surrounding commas or slashes are
        removed.
    """
    kept = []
    for node in _significant(value):
        if _is_colour(node) or _is_gradient(node):
            continue
        if drop_images and _is_image(node):
            continue
        kept.append(node)
    while kept and (_is_literal(kept[0], ",") or _is_literal(kept[0], "/")):
        kept.pop(0)
    while kept and (_is_literal(kept[-1], ",") or _is_literal(kept[-1], "/")):
        kept.pop()
    if not kept:
        return None
    text = ""
    for node in kept:
        if text and not _is_literal(node, ","):
            text += " "
        text += tinycss2.serialize([node])
    rebuilt: list[Any] = tinycss2.parse_component_value_list(" " + text)
    return rebuilt


def has_absolute_length(value: list[Any]) -> bool:
    """Check if any component value is a dimension token in an absolute unit.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        True if the list contains at least one dimension token with an absolute unit.
    """
    return any(node.type == "dimension" and is_absolute_unit(node.lower_unit) for node in value)


def is_viewport_length(value: list[Any]) -> bool:
    """Check if any component value is a dimension in a viewport-relative unit.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        True if the list contains a dimension token in one of VIEWPORT_UNITS.
    """
    return any(node.type == "dimension" and node.lower_unit in VIEWPORT_UNITS for node in value)


def convert_lengths(value: list[Any], *, base_px: float) -> tuple[list[Any], bool]:
    """Replace every absolute-length dimension token with its em equivalent.

    Args:
        value: A tinycss2 component-value list.
        base_px: The font size in px that 1em stands for.

    Returns:
        A tuple of (modified_value_list, changed_flag). changed_flag is True if
        any tokens were replaced.
    """
    out: list[Any] = []
    changed = False
    for node in value:
        replacement = (
            to_em(node.value, node.lower_unit, base_px=base_px)
            if node.type == "dimension" and is_absolute_unit(node.lower_unit)
            else None
        )
        if replacement is None:
            out.append(node)
            continue
        out.extend(tinycss2.parse_component_value_list(replacement))
        changed = True
    return out, changed
