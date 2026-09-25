"""EPUB normalization at the stylesheet level (FR-NORM-2, TR-NORM-4).

This module rewrites a CSS stylesheet, handling at-rules (`@media`, `@supports`,
`@font-face`, etc.), recursively rewriting nested declarations, and removing
orphaned rules and at-rules that end up empty after declaration stripping.

**Two-level walk:**

The stylesheet is walked as a list of top-level nodes (qualified rules, at-rules,
comments, whitespace). `@font-face` rules are dropped when font stripping is
enabled. `@media` and `@supports` blocks are recursively descended into: their
nested rules are rewritten (declarations normalized, empty rules dropped), and
if the block ends up with no content, the entire at-rule is dropped (orphan
cleanup, `TR-NORM-4`). Any other at-rule (`@page`, `@import`, `@charset`,
`@namespace`, or unknown) is kept verbatim and never recursed into, because an
unrecognized at-rule's grammar is unknown — its "declarations" may not be
declarations and could be malformed if recursed.

Qualified rules have their declarations rewritten and are dropped if no
declarations survive the pass (orphan cleanup). Comments, whitespace, and
parse errors are preserved verbatim.

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

from typing import Any

import tinycss2

from .counts import NormalizeCounts
from .declarations import (
    css_text,
    normalize_declarations,
)
from .options import NormalizeOptions


def normalize_stylesheet(
    css: str,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
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

    Returns:
        The rewritten CSS text, or the input verbatim if nothing changed.
    """
    before = (
        counts.declarations_removed,
        counts.declarations_converted,
        counts.rules_removed,
    )
    nodes = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
    out = _rewrite_rules(nodes, options=options, counts=counts)
    after = (
        counts.declarations_removed,
        counts.declarations_converted,
        counts.rules_removed,
    )
    if before == after:
        return css
    return css_text(out)


def _rewrite_rules(
    nodes: list[Any],
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
) -> list[Any]:
    """Walk a rule list and return survivors after rewriting and orphan cleanup.

    Processes each node: @font-face rules are dropped if `strip_fonts` is enabled;
    @media/@supports blocks are recursively descended with their content rewritten,
    and dropped if empty; qualified rules have declarations rewritten and are
    dropped if no declarations survive; other at-rules are kept verbatim;
    whitespace/comments/errors are preserved.

    Args:
        nodes: A tinycss2 node list from parse_stylesheet() or parse_rule_list().
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.

    Returns:
        The filtered and transformed node list.
    """
    out: list[Any] = []
    for node in nodes:
        if node.type == "at-rule":
            result = _rewrite_at_rule(node, options=options, counts=counts)
            if result is not None:
                out.append(result)
        elif node.type == "qualified-rule":
            selector = css_text(node.prelude).strip()
            decls = tinycss2.parse_blocks_contents(
                node.content, skip_comments=False, skip_whitespace=False
            )
            survivors = normalize_declarations(
                decls, options=options, selector=selector, counts=counts
            )
            if any(n.type == "declaration" for n in survivors):
                node.content = survivors
                out.append(node)
            else:
                counts.rules_removed += 1
        else:
            out.append(node)
    return out


def _rewrite_at_rule(
    node: Any,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
) -> Any | None:
    """Rewrite or drop an at-rule, returning None if it should be removed.

    Handles @font-face (dropped if strip_fonts enabled), @media/@supports
    (recursively descended with orphan cleanup), and other at-rules (kept verbatim).

    Args:
        node: A tinycss2 AtRule node.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.

    Returns:
        The at-rule node (possibly mutated in place), or None if it should be dropped.
    """
    lower_at_keyword = node.lower_at_keyword

    if lower_at_keyword == "font-face" and options.strip_fonts:
        counts.rules_removed += 1
        return None

    if lower_at_keyword in {"media", "supports"} and node.content is not None:
        inner = tinycss2.parse_rule_list(node.content, skip_comments=False, skip_whitespace=False)
        new_inner = _rewrite_rules(inner, options=options, counts=counts)
        if any(n.type in {"qualified-rule", "at-rule"} for n in new_inner):
            node.content = new_inner
            return node
        counts.rules_removed += 1
        return None

    return node
