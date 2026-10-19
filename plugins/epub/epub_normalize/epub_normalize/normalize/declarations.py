"""EPUB normalization at the declaration level (FR-NORM-3..10, FR-NORM-16, FR-NORM-17).

This module rewrites one CSS declaration list under the context of its rule (``RuleContext``),
the rule groups of the level (``NormalizeOptions``) and the book's body-text size
(``BookContext``).

**The decision tree:**

Each declaration meets these steps in this order, and the first step that applies decides.
The order matters because some properties belong to several groups: ``font`` is expanded
before its longhands meet the size, spacing and weight rules, and the root's page layout is
removed before a margin could be converted.

0. A vector context (SVG, MathML) is returned unchanged.
1. A drop cap keeps every declaration but its colours, or, with ``plain_drop_caps``, only the
   ``SEMANTIC_PROPERTIES``.
2. Colour longhands and shadows are removed.
3. Colour shorthands lose their colour and gradient components (and, for the root's
   ``background``, their images); one left empty is removed.
4. ``background-image`` is removed for a gradient, and for an image on the root.
5. ``font-family`` is removed unless it names a monospace family, which becomes ``monospace``.
6. The ``font`` shorthand is replaced by its longhands, each of which then meets steps 7-14.
7. ``font-size`` is removed on the root, in viewport units, below the heading level at the
   strict level, or when it equals the body-text size; any other absolute size becomes ``em``.
8. Spacing and 9. hyphenation are removed.
10. A body-text alignment is removed.
11. The root's page layout is removed.
12. Absolute fixed dimensions are removed outside image contexts.
13. Absolute margins, paddings and indents become ``em`` outside image and root contexts.
14. Anything else is kept.

A kept declaration moves no counter, so a second pass over a normalized rule changes nothing;
a removal counts in ``declarations_removed`` and a rewrite in ``declarations_converted``.

``css_text`` exists purely as a typed façade over an untyped library:
``tinycss2.serialize`` returns ``Any``, but callers need ``str``.
"""

from __future__ import annotations

from typing import Any, Literal

import tinycss2

from .counts import NormalizeCounts
from .options import NormalizeOptions
from .rules import (
    ALIGNMENT_PROPERTIES,
    BACKGROUND_IMAGE_PROPERTY,
    BODY_ALIGNMENTS,
    BOX_PROPERTIES,
    COLOR_PROPERTIES,
    COLOR_SHORTHAND_PROPERTIES,
    FIXED_SIZE_PROPERTIES,
    FONT_FAMILY_PROPERTY,
    FONT_SHORTHAND_PROPERTY,
    FONT_SIZE_PROPERTY,
    GRADIENT_FUNCTIONS,
    HYPHENATION_PROPERTIES,
    ROOT_LAYOUT_PROPERTIES,
    SEMANTIC_PROPERTIES,
    SPACING_PROPERTIES,
)
from .selectors import RuleContext
from .survey import BookContext, parse_font_size
from .values import (
    convert_lengths,
    expand_font_shorthand,
    has_absolute_length,
    is_monospace,
    is_viewport_length,
    strip_colors,
)

Decision = Literal["drop", "keep", "convert"]
"""What a step decided: remove the declaration, keep it untouched, or keep it rewritten."""


def css_text(nodes: list[Any]) -> str:
    """Serialize a tinycss2 node list to CSS text.

    Args:
        nodes: A list of tinycss2 AST nodes.

    Returns:
        The serialized CSS text.
    """
    return str(tinycss2.serialize(nodes))


def _significant(value: list[Any]) -> list[Any]:
    """The tokens of a component-value list that are neither whitespace nor comments.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        The remaining tokens, in order.
    """
    return [token for token in value if token.type not in ("whitespace", "comment")]


def _has_gradient(value: list[Any]) -> bool:
    """Check whether a value contains a gradient function.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        True if a top-level token is a function naming a gradient.
    """
    return any(t.type == "function" and t.lower_name in GRADIENT_FUNCTIONS for t in value)


def _has_image(value: list[Any]) -> bool:
    """Check whether a value contains a ``url`` token or a ``url()`` function.

    Args:
        value: A tinycss2 component-value list.

    Returns:
        True if a top-level token is a url.
    """
    return any(t.type == "url" or (t.type == "function" and t.lower_name == "url") for t in value)


def _colour_longhand_step(node: Any, *, options: NormalizeOptions) -> Decision | None:
    """Step 2: colour longhands and shadows are removed.

    Args:
        node: The tinycss2 declaration.
        options: The level's rule groups.

    Returns:
        ``"drop"`` for a colour-bearing property, else ``None``.
    """
    if options.strip_colors and node.lower_name in COLOR_PROPERTIES:
        return "drop"
    return None


def _colour_shorthand_step(
    node: Any, *, options: NormalizeOptions, context: RuleContext
) -> Decision | None:
    """Step 3: colour shorthands lose their colour components.

    Args:
        node: The tinycss2 declaration; its value is replaced when components are removed.
        options: The level's rule groups.
        context: The rule's context (the root's ``background`` also loses its images).

    Returns:
        ``"drop"`` when nothing is left, ``"convert"`` when components were removed,
        ``"keep"`` when the value had none, else ``None`` for another property.
    """
    name = node.lower_name
    if not options.strip_colors or name not in COLOR_SHORTHAND_PROPERTIES:
        return None
    stripped = strip_colors(node.value, drop_images=context.root and name == "background")
    if stripped is None:
        return "drop"
    if len(_significant(stripped)) < len(_significant(node.value)):
        node.value = stripped
        return "convert"
    return "keep"


def _background_image_step(
    node: Any, *, options: NormalizeOptions, context: RuleContext
) -> Decision | None:
    """Step 4: gradient backgrounds, and image backgrounds on the root, are removed.

    Args:
        node: The tinycss2 declaration.
        options: The level's rule groups.
        context: The rule's context.

    Returns:
        ``"drop"`` or ``"keep"`` for ``background-image``, else ``None``.
    """
    if not options.strip_colors or node.lower_name != BACKGROUND_IMAGE_PROPERTY:
        return None
    if _has_gradient(node.value) or (context.root and _has_image(node.value)):
        return "drop"
    return "keep"


def _font_family_step(node: Any, *, options: NormalizeOptions) -> Decision | None:
    """Step 5: only a monospace family list survives, as ``monospace``.

    Args:
        node: The tinycss2 declaration; its value is replaced by ``monospace``.
        options: The level's rule groups.

    Returns:
        ``"drop"`` for a non-monospace list, ``"keep"`` for a bare ``monospace``,
        ``"convert"`` for any other monospace list, else ``None`` for another property.
    """
    if not options.strip_fonts or node.lower_name != FONT_FAMILY_PROPERTY:
        return None
    if not is_monospace(node.value):
        return "drop"
    tokens = [t for t in node.value if t.type != "whitespace"]
    if len(tokens) == 1 and tokens[0].type == "ident" and tokens[0].lower_value == "monospace":
        return "keep"
    node.value = tinycss2.parse_component_value_list(" monospace")
    return "convert"


def _font_size_step(
    node: Any, *, options: NormalizeOptions, context: RuleContext, book: BookContext
) -> Decision | None:
    """Step 7: a font size is removed, converted to ``em``, or kept.

    Args:
        node: The tinycss2 declaration; its value is replaced when converted.
        options: The level's rule groups.
        context: The rule's context.
        book: The book's body-text size.

    Returns:
        A decision for ``font-size`` when sizes are normalized, else ``None``.
    """
    if not options.convert_font_sizes or node.lower_name != FONT_SIZE_PROPERTY:
        return None
    value = node.value
    if context.root or (options.headings_only_sizes and not context.heading):
        return "drop"
    if is_viewport_length(value):
        return "drop"
    size = parse_font_size(value)
    if book.base_size is not None and size is not None and size.same_as(book.base_size):
        return "drop"
    if has_absolute_length(value):
        node.value, changed = convert_lengths(value, base_px=book.base_px)
        return "convert" if changed else "keep"
    return "keep"


def _removal_step(node: Any, *, options: NormalizeOptions, context: RuleContext) -> Decision | None:
    """Steps 8-12: spacing, hyphenation, body alignment, root layout and fixed dimensions.

    Args:
        node: The tinycss2 declaration.
        options: The level's rule groups.
        context: The rule's context.

    Returns:
        ``"drop"`` when one of the removal rules applies, else ``None``.
    """
    name, value = node.lower_name, node.value
    if options.strip_line_spacing and name in SPACING_PROPERTIES:
        return "drop"
    if options.strip_hyphenation and name in HYPHENATION_PROPERTIES:
        return "drop"
    if options.strip_text_alignment and name in ALIGNMENT_PROPERTIES:
        words = _significant(value)
        if len(words) == 1 and words[0].type == "ident" and words[0].lower_value in BODY_ALIGNMENTS:
            return "drop"
    if options.strip_page_layout and context.root and name in ROOT_LAYOUT_PROPERTIES:
        return "drop"
    if (
        options.strip_fixed_dimensions
        and not context.image
        and name in FIXED_SIZE_PROPERTIES
        and has_absolute_length(value)
    ):
        return "drop"
    return None


def _margin_step(
    node: Any, *, options: NormalizeOptions, context: RuleContext, book: BookContext
) -> Decision | None:
    """Step 13: absolute margins, paddings and indents become ``em``.

    Args:
        node: The tinycss2 declaration; its value is replaced when converted.
        options: The level's rule groups.
        context: The rule's context.
        book: The book's body-text size.

    Returns:
        ``"convert"`` when a length was rewritten, else ``None``.
    """
    if (
        options.normalize_margins
        and not context.image
        and not context.root
        and node.lower_name in BOX_PROPERTIES
        and has_absolute_length(node.value)
    ):
        node.value, changed = convert_lengths(node.value, base_px=book.base_px)
        return "convert" if changed else None
    return None


def _decide_late(
    node: Any, *, options: NormalizeOptions, context: RuleContext, book: BookContext
) -> Decision:
    """Steps 7-14 for one declaration.

    Args:
        node: The tinycss2 declaration.
        options: The level's rule groups.
        context: The rule's context.
        book: The book's body-text size.

    Returns:
        The first applicable decision, ``"keep"`` when none applies.
    """
    decision = _font_size_step(node, options=options, context=context, book=book)
    decision = decision or _removal_step(node, options=options, context=context)
    decision = decision or _margin_step(node, options=options, context=context, book=book)
    return decision or "keep"


def _settle(decision: Decision, counts: NormalizeCounts) -> bool:
    """Count a decision.

    Args:
        decision: What the tree decided for a declaration.
        counts: Accumulator for tracking changes.

    Returns:
        True if the declaration stays in the output.
    """
    if decision == "drop":
        counts.declarations_removed += 1
        return False
    if decision == "convert":
        counts.declarations_converted += 1
    return True


def _expand_font(
    node: Any,
    *,
    options: NormalizeOptions,
    context: RuleContext,
    book: BookContext,
    counts: NormalizeCounts,
) -> list[Any]:
    """Step 6: replace a ``font`` shorthand by its longhands, which meet steps 7-14.

    Args:
        node: The tinycss2 ``font`` declaration.
        options: The level's rule groups.
        context: The rule's context.
        book: The book's body-text size.
        counts: Accumulator for tracking changes.

    Returns:
        The surviving longhand declarations, separated by one whitespace node; empty when the
        shorthand cannot be read.
    """
    longhands = expand_font_shorthand(node.value)
    if longhands is None:
        counts.declarations_removed += 1
        return []
    counts.declarations_converted += 1
    text = "; ".join(f"{prop}: {value}" for prop, value in longhands)
    expanded = tinycss2.parse_blocks_contents(text, skip_comments=False, skip_whitespace=False)
    out: list[Any] = []
    for new in expanded:
        if new.type != "declaration":
            continue
        new.important = node.important
        if _settle(_decide_late(new, options=options, context=context, book=book), counts):
            if out:
                out.append(tinycss2.parse_component_value_list(" ")[0])
            out.append(new)
    return out


def _rewrite_drop_cap(
    node: Any, *, options: NormalizeOptions, context: RuleContext, counts: NormalizeCounts
) -> bool:
    """Step 1: decide one declaration of a drop cap.

    Args:
        node: The tinycss2 declaration.
        options: The level's rule groups.
        context: The rule's context.
        counts: Accumulator for tracking changes.

    Returns:
        True if the declaration stays in the output.
    """
    if options.plain_drop_caps:
        return _settle("keep" if node.lower_name in SEMANTIC_PROPERTIES else "drop", counts)
    decision = _colour_longhand_step(node, options=options)
    decision = decision or _colour_shorthand_step(node, options=options, context=context)
    return _settle(decision or "keep", counts)


def _rewrite(
    node: Any,
    *,
    options: NormalizeOptions,
    context: RuleContext,
    book: BookContext,
    counts: NormalizeCounts,
) -> list[Any]:
    """Run one declaration through the tree.

    Args:
        node: The tinycss2 declaration.
        options: The level's rule groups.
        context: The rule's context.
        book: The book's body-text size.
        counts: Accumulator for tracking changes.

    Returns:
        The nodes that replace it: the declaration itself, its longhands, or nothing.
    """
    if context.drop_cap:
        return (
            [node]
            if _rewrite_drop_cap(node, options=options, context=context, counts=counts)
            else []
        )
    if options.strip_fonts and node.lower_name == FONT_SHORTHAND_PROPERTY:
        return _expand_font(node, options=options, context=context, book=book, counts=counts)
    decision = _colour_longhand_step(node, options=options)
    decision = decision or _colour_shorthand_step(node, options=options, context=context)
    decision = decision or _background_image_step(node, options=options, context=context)
    decision = decision or _font_family_step(node, options=options)
    decision = decision or _decide_late(node, options=options, context=context, book=book)
    return [node] if _settle(decision, counts) else []


def normalize_declarations(
    nodes: list[Any],
    *,
    options: NormalizeOptions,
    context: RuleContext,
    book: BookContext,
    counts: NormalizeCounts,
) -> list[Any]:
    """Filter and transform one declaration list under its rule's context.

    Applies the decision tree of the module docstring to each declaration. Non-declaration
    nodes (whitespace, comments, parse errors) are kept verbatim.

    Mutations:
        - Modifies ``counts.declarations_removed`` and ``counts.declarations_converted``.
        - Replaces the ``.value`` of converted declaration nodes in place.

    Args:
        nodes: A tinycss2 node list from ``parse_blocks_contents()``.
        options: The level's rule groups.
        context: The context of the rule the declarations belong to.
        book: The book's body-text size.
        counts: Accumulator for tracking changes.

    Returns:
        The filtered and transformed node list.
    """
    if context.vector:
        return nodes
    out: list[Any] = []
    for node in nodes:
        if node.type == "declaration":
            out.extend(_rewrite(node, options=options, context=context, book=book, counts=counts))
        else:
            out.append(node)
    return out
