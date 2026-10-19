"""EPUB normalization at the stylesheet level (FR-NORM-2, FR-NORM-3, FR-NORM-17, TR-NORM-4).

This module rewrites a CSS stylesheet, handling at-rules (`@media`, `@supports`,
`@font-face`, `@page`, etc.), recursively rewriting nested declarations, and removing
orphaned rules and at-rules that end up empty after declaration stripping.

**Two-level walk:**

The stylesheet is walked as a list of top-level nodes (qualified rules, at-rules,
comments, whitespace). `@font-face` rules are dropped when font stripping is enabled,
unless the book uses their family in a drop cap or in vector content
(`BookContext.kept_families`). `@page` blocks lose their margin and padding
descriptors when page layout stripping is enabled (`FR-NORM-17`). `@media` and
`@supports` blocks are recursively descended into: their nested rules are rewritten
(declarations normalized, empty rules dropped), and if the block ends up with no
content, the entire at-rule is dropped (orphan cleanup, `TR-NORM-4`). Any other
at-rule (`@import`, `@charset`, `@namespace`, or unknown) is kept verbatim and never
recursed into, because an unrecognized at-rule's grammar is unknown — its
"declarations" may not be declarations and could be malformed if recursed.

Qualified rules have their declarations rewritten under the context of their
selectors. A selector list whose selectors differ in context is split into one rule
per context (`TR-NORM-6`), each keeping only the declarations that context allows;
a rule whose declarations survive in no context is dropped (orphan cleanup).
Comments, whitespace, and parse errors are preserved verbatim.

**Fidelity rule:**

If none of the three counters (`declarations_removed`, `declarations_converted`,
`rules_removed`) moved during this pass, the function returns the input CSS
verbatim, never the re-serialized text. This is critical for idempotency:
`tinycss2.parse_stylesheet` silently drops CSS CDO/CDC tokens (`<!--`, `-->`),
so a round-trip would lose those bytes. Returning the input verbatim ensures
a second normalization pass is a guaranteed byte-identical no-op, which
`FR-NORM-13` requires.

**Mutation:**

Modifies the `counts` accumulator in place. Does not modify the input CSS string.
"""

from __future__ import annotations

import logging
from typing import Any

import tinycss2
from tinycss2.ast import QualifiedRule

from .counts import NormalizeCounts
from .declarations import (
    css_text,
    normalize_declarations,
)
from .options import NormalizeOptions
from .rules import PAGE_MARGIN_DESCRIPTORS
from .selectors import RuleContext, group_by_context, parse_selector_list
from .survey import BookContext
from .values import family_names

logger = logging.getLogger(__name__)


def normalize_stylesheet(
    css: str,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    book: BookContext | None = None,
) -> str:
    """Rewrite a CSS stylesheet, normalizing at-rules and declarations.

    Walks the stylesheet as a list of top-level nodes, recursively descending
    into nesting at-rules (`@media`, `@supports`), normalizing declarations
    in qualified rules, and removing orphaned rules and blocks. Returns the
    input verbatim (never re-serialized) if no counters moved, ensuring the
    fidelity rule: a second normalization pass is byte-identical.

    Args:
        css: The CSS stylesheet text.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes across multiple files.
        book: What the survey learned about the book; an empty ``BookContext`` when omitted.

    Returns:
        The rewritten CSS text, or the input verbatim if nothing changed.
    """
    book = BookContext() if book is None else book
    before = _counters(counts)
    nodes = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
    out = _rewrite_rules(nodes, options=options, counts=counts, book=book)
    if before == _counters(counts):
        return css
    return css_text(out)


def _counters(counts: NormalizeCounts) -> tuple[int, int, int]:
    """Read the three counters whose movement decides whether a pass changed anything.

    Args:
        counts: The accumulator to read.

    Returns:
        ``(declarations_removed, declarations_converted, rules_removed)``.
    """
    return (counts.declarations_removed, counts.declarations_converted, counts.rules_removed)


def _rewrite_rules(
    nodes: list[Any],
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    book: BookContext,
) -> list[Any]:
    """Walk a rule list and return survivors after rewriting and orphan cleanup.

    Processes each node: at-rules go through `_rewrite_at_rule`; qualified rules go
    through `_rewrite_qualified` and may come back split or dropped; whitespace,
    comments and errors are preserved.

    Args:
        nodes: A tinycss2 node list from parse_stylesheet() or parse_rule_list().
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.
        book: What the survey learned about the book.

    Returns:
        The filtered and transformed node list.
    """
    out: list[Any] = []
    for node in nodes:
        if node.type == "at-rule":
            result = _rewrite_at_rule(node, options=options, counts=counts, book=book)
            if result is not None:
                out.append(result)
        elif node.type == "qualified-rule":
            out.extend(_rewrite_qualified(node, options=options, counts=counts, book=book))
        else:
            out.append(node)
    return out


def _rewrite_qualified(
    node: Any,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    book: BookContext,
) -> list[Any]:
    """Rewrite one qualified rule, splitting it when its selectors differ in context.

    A single context rewrites the rule in place, or drops it when no declaration
    survives. Several contexts normalise a fresh copy of the declarations under each
    one; if nothing changed the rule is kept as it was, otherwise it is replaced by
    one rule per context that kept a declaration.

    Args:
        node: A tinycss2 QualifiedRule node.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.
        book: What the survey learned about the book.

    Returns:
        The replacement nodes: none when the rule empties, the rule itself, or the
        rules a split produces.
    """
    groups = group_by_context(parse_selector_list(node.prelude), book) or [(RuleContext(), [])]
    if len(groups) == 1:
        survivors = _normalized_block(node, groups[0][0], options=options, counts=counts, book=book)
        if any(n.type == "declaration" for n in survivors):
            node.content = survivors
            return [node]
        counts.rules_removed += 1
        return []

    before = _counters(counts)
    results = [
        (selectors, _normalized_block(node, context, options=options, counts=counts, book=book))
        for context, selectors in groups
    ]
    if before == _counters(counts):
        if any(n.type == "declaration" for n in results[0][1]):
            return [node]
        counts.rules_removed += 1
        return []

    counts.declarations_converted += 1
    logger.debug(
        "Normalize split a rule into %d by selector context: %s",
        len(groups),
        tinycss2.serialize(node.prelude).strip(),
    )
    out: list[Any] = []
    for selectors, survivors in results:
        if not any(n.type == "declaration" for n in survivors):
            counts.rules_removed += 1
            continue
        prelude = tinycss2.parse_component_value_list(", ".join(s.text for s in selectors) + " ")
        out.append(QualifiedRule(node.source_line, node.source_column, prelude, survivors))
    return out


def _normalized_block(
    node: Any,
    context: RuleContext,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    book: BookContext,
) -> list[Any]:
    """Normalize a fresh copy of a qualified rule's declarations under one context.

    Args:
        node: A tinycss2 QualifiedRule node; its content is read, never changed.
        context: The selector context the declarations are judged under.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.
        book: What the survey learned about the book.

    Returns:
        The declaration and comment nodes that survive under *context*.
    """
    decls = tinycss2.parse_blocks_contents(node.content, skip_comments=False, skip_whitespace=False)
    return normalize_declarations(decls, options=options, context=context, book=book, counts=counts)


def _rewrite_at_rule(
    node: Any,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    book: BookContext,
) -> Any | None:
    """Rewrite or drop an at-rule, returning None if it should be removed.

    Handles @font-face (dropped if strip_fonts enabled, unless its family is kept),
    @page (margins removed when strip_page_layout is enabled), @media/@supports
    (recursively descended with orphan cleanup), and other at-rules (kept verbatim).

    Args:
        node: A tinycss2 AtRule node.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.
        book: What the survey learned about the book.

    Returns:
        The at-rule node (possibly mutated in place), or None if it should be dropped.
    """
    lower_at_keyword = node.lower_at_keyword

    if lower_at_keyword == "font-face" and options.strip_fonts:
        if _kept_font_face(node, book):
            return node
        counts.rules_removed += 1
        return None

    if lower_at_keyword == "page" and options.strip_page_layout and node.content is not None:
        return _rewrite_page(node, counts=counts)

    if lower_at_keyword in {"media", "supports"} and node.content is not None:
        inner = tinycss2.parse_rule_list(node.content, skip_comments=False, skip_whitespace=False)
        new_inner = _rewrite_rules(inner, options=options, counts=counts, book=book)
        if any(n.type in {"qualified-rule", "at-rule"} for n in new_inner):
            node.content = new_inner
            return node
        counts.rules_removed += 1
        return None

    return node


def _kept_font_face(node: Any, book: BookContext) -> bool:
    """Whether an @font-face's family is one the book keeps for a drop cap or vector content.

    Args:
        node: A tinycss2 AtRule node for an @font-face.
        book: What the survey learned about the book.

    Returns:
        True when the family names of its ``font-family`` descriptor meet
        ``book.kept_families``; False when there is no such descriptor.
    """
    if node.content is None:
        return False
    for item in tinycss2.parse_blocks_contents(
        node.content, skip_comments=True, skip_whitespace=True
    ):
        if item.type == "declaration" and item.lower_name == "font-family":
            return any(name in book.kept_families for name in family_names(item.value))
    return False


def _rewrite_page(node: Any, *, counts: NormalizeCounts) -> Any | None:
    """Remove the margin and padding descriptors from an @page block (FR-NORM-17).

    Nested at-rules such as ``@top-center`` and the other declarations stay. An @page
    left with neither a declaration nor a nested at-rule is dropped.

    Args:
        node: A tinycss2 AtRule node for an @page with a block.
        counts: Accumulator for tracking changes.

    Returns:
        The at-rule node with its block rebuilt, or None when the block is empty.
    """
    kept: list[Any] = []
    for item in tinycss2.parse_blocks_contents(
        node.content, skip_comments=False, skip_whitespace=False
    ):
        if item.type == "declaration" and item.lower_name in PAGE_MARGIN_DESCRIPTORS:
            counts.declarations_removed += 1
        else:
            kept.append(item)
    if not any(item.type in {"declaration", "at-rule"} for item in kept):
        counts.rules_removed += 1
        return None
    node.content = kept
    return node
