"""EPUB normalization at the declaration level (FR-NORM-1, FR-NORM-13, TR-NORM-1).

This module rewrites a CSS declaration list, filtering and transforming properties
based on the normalization options and the selector context.

**The branch decision tree:**

Every declaration is evaluated in this exact order. The order matters because
some properties appear in multiple sets: `text-indent` is both an alignment
property and a box property, so the alignment check (preserve by default) runs
before the box check (convert to em when enabled). Other properties like
`max-width` are never stripped: they appear in the never-stripped set for
every condition to check early and skip evaluation.

The preserved-selector check short-circuits the entire list rather than
filtering per declaration, because a drop-cap rule needs all its styling
intact: a large absolute size, a specific family, and a colour. These all
exist precisely to override the surrounding document's defaults, so checking
them per-property would be fragile.

`css_text` exists purely as a typed façade over an untyped library:
`tinycss2.serialize` returns `Any`, but callers need `str`. Wrapping it
provides type safety without hiding the dependency.
"""

from __future__ import annotations

from typing import Any

import tinycss2

from .counts import NormalizeCounts
from .options import NormalizeOptions
from .rules import (
    ALIGNMENT_PROPERTIES,
    BOX_PROPERTIES,
    COLOR_PROPERTIES,
    COLOR_SHORTHAND_PROPERTIES,
    DIMENSION_PROPERTIES,
    FONT_PROPERTIES,
    FONT_SIZE_PROPERTY,
    NEVER_STRIPPED_PROPERTIES,
    SPACING_PROPERTIES,
    is_image_selector,
    is_preserved_selector,
    is_root_selector,
)
from .units import is_absolute_unit, to_em


def css_text(nodes: list[Any]) -> str:
    """Serialize a tinycss2 node list to CSS text.

    Args:
        nodes: A list of tinycss2 AST nodes.

    Returns:
        The serialized CSS text.
    """
    return str(tinycss2.serialize(nodes))


def has_absolute_length(value: list[Any]) -> bool:
    """Check if any component value is a dimension token in an absolute unit.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        True if the list contains at least one dimension token with an absolute unit.
    """
    return any(node.type == "dimension" and is_absolute_unit(node.lower_unit) for node in value)


def _convert_lengths(value: list[Any]) -> tuple[list[Any], bool]:
    """Replace every absolute-length dimension token with its em equivalent.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        A tuple of (modified_value_list, changed_flag). changed_flag is True if
        any tokens were replaced.
    """
    out: list[Any] = []
    changed = False
    for node in value:
        replacement = (
            to_em(node.value, node.lower_unit)
            if node.type == "dimension" and is_absolute_unit(node.lower_unit)
            else None
        )
        if replacement is None:
            out.append(node)
            continue
        out.extend(tinycss2.parse_component_value_list(replacement))
        changed = True
    return out, changed


def _should_drop(
    name: str,
    node: Any,
    *,
    options: NormalizeOptions,
    selector: str,
) -> bool:
    """Decide whether a declaration should be dropped from the output.

    Evaluates the declaration against the normalization options and selector
    context, applying the full branch decision tree. Returns True if the
    declaration should be removed, False if it should be kept (possibly with
    modifications applied in-place by the caller).

    Args:
        name: The property name (lowercase via node.lower_name).
        node: The tinycss2 Declaration node.
        options: Normalization options controlling which properties to strip/convert.
        selector: The CSS selector for the rule containing this declaration.

    Returns:
        True if the declaration should be dropped, False otherwise.
    """
    # Never-stripped properties are always kept.
    if name in NEVER_STRIPPED_PROPERTIES:
        return False

    # Font properties
    if options.strip_fonts and name in FONT_PROPERTIES:
        return True

    # Color properties
    if options.strip_colors and name in COLOR_PROPERTIES:
        return True

    # Color shorthands (only if no url() in value)
    if (
        options.strip_colors
        and name in COLOR_SHORTHAND_PROPERTIES
        and "url(" not in css_text(node.value).lower()
    ):
        return True

    # Line spacing properties
    if options.strip_line_spacing and name in SPACING_PROPERTIES:
        return True

    # Dimension properties (absolute lengths only, not on image selectors)
    if (
        options.strip_fixed_dimensions
        and name in DIMENSION_PROPERTIES
        and not is_image_selector(selector)
        and has_absolute_length(node.value)
    ):
        return True

    # Alignment properties (inverse: preserved by default)
    if not options.preserve_alignment and name in ALIGNMENT_PROPERTIES:
        return True

    # Font size on root selectors is stripped (not converted)
    return options.convert_font_sizes and name == FONT_SIZE_PROPERTY and is_root_selector(selector)


def normalize_declarations(
    nodes: list[Any],
    *,
    options: NormalizeOptions,
    selector: str,
    counts: NormalizeCounts,
) -> list[Any]:
    """Filter and transform a CSS declaration list based on normalization options.

    Applies the normalization decision tree to each declaration in the input list.
    Non-declaration nodes (whitespace, comments, parse errors) are kept verbatim.
    Preserved selectors bypass normalization entirely and return the list unchanged.

    Mutations:
        - Modifies `counts.declarations_removed` and `counts.declarations_converted`.
        - May modify the `.value` field of declaration nodes in-place when converting lengths.

    Args:
        nodes: A tinycss2 node list from parse_blocks_contents().
        options: Normalization options controlling which properties to strip/convert.
        selector: The CSS selector for the rule containing these declarations.
        counts: Accumulator for tracking changes.

    Returns:
        The filtered and transformed node list.
    """
    # Short-circuit for preserved selectors.
    if is_preserved_selector(selector):
        return nodes

    out: list[Any] = []
    for node in nodes:
        # Keep non-declaration nodes verbatim.
        if node.type != "declaration":
            out.append(node)
            continue

        name = node.lower_name
        value = node.value

        # Check if this declaration should be dropped.
        if _should_drop(name, node, options=options, selector=selector):
            counts.declarations_removed += 1
            continue

        # Font size conversion (not on root selectors, which are dropped above).
        if options.convert_font_sizes and name == FONT_SIZE_PROPERTY:
            new_value, changed = _convert_lengths(value)
            if changed:
                node.value = new_value
                counts.declarations_converted += 1
            out.append(node)
            continue

        # Box property conversion (margin, padding, text-indent).
        if options.normalize_margins and name in BOX_PROPERTIES:
            new_value, changed = _convert_lengths(value)
            if changed:
                node.value = new_value
                counts.declarations_converted += 1
            out.append(node)
            continue

        # Keep the declaration (already filtered by _should_drop).
        out.append(node)

    return out
