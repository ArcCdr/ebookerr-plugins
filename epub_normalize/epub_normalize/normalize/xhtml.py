"""Surgical rewriting of XHTML chapter documents (style blocks, attributes, viewport meta).

This module rewrites chapter documents by targeted text substitution over comment-masked source,
avoiding XML parsing altogether. This is critical for two reasons:

1. **Tolerance of structural defects** (`TR-NORM-1`): stdlib `ElementTree` silently drops the
   DOCTYPE, comments and processing instructions on round-trip, and refuses outright to parse a
   chapter with a structural defect (e.g., mismatched tags) or an HTML entity such as `&nbsp;`.
   Real EPUB books commonly have both, so rewriting the raw text ensures we tolerate what readers
   must tolerate.

2. **The masking guard** — before touching `style=""` attributes, we mask out every HTML comment
   and every `<style>` block with index placeholders. This prevents a substring like `style="…"`
   that merely *appears* inside a comment or inside CSS from being incorrectly rewritten. The
   order matters: mask first, rewrite attributes, then unmask.

**Well-formedness as a relative guard** (`TR-NORM-2`): The before/after well-formedness probe is
a *relative* check, not an absolute validity claim. A chapter that was well-formed XML before the
rewrite but is not after has had a genuine regression — it must be reverted. A chapter that
already failed to parse (e.g., because of `&nbsp;` or mismatched tags) is rewritten without
re-parsing, because the regression guard only fires on a *change in status*.

**Inline style declarations and empty selector** — A `style=""` attribute is normalized with an
empty selector (`selector=""`), so an inline `font-size` on `<body>` is *converted* to relative
units rather than removed. The element name is not knowable from an attribute match alone, and
converting is the conservative outcome (preserves the intent when possible rather than dropping
the declaration).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import tinycss2
from ebookerr_sdk.epub.errors import MalformedEpubError
from ebookerr_sdk.epub.safe_xml import safe_fromstring

from .counts import NormalizeCounts
from .declarations import (
    css_text,
    normalize_declarations,
)
from .options import NormalizeOptions
from .stylesheet import normalize_stylesheet

logger = logging.getLogger(__name__)

_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL)
_MASK_RE = re.compile(r"<!--.*?-->|<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")
_STYLE_ATTR_RE = re.compile(r"(\s)style\s*=\s*([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)
_VIEWPORT_META_RE = re.compile(
    r"[ \t]*<meta\b[^>]*\bname\s*=\s*[\"']viewport[\"'][^>]*>[ \t]*\n?",
    re.IGNORECASE,
)


def is_well_formed(text: str) -> bool:
    """Test whether text parses as well-formed XML.

    Args:
        text: The XML text to test.

    Returns:
        True if the text parses without entity/DTD attacks or structural errors,
        False otherwise.
    """
    try:
        safe_fromstring(text)
        return True
    except (MalformedEpubError, ET.ParseError):
        return False


def normalize_xhtml(
    text: str,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    name: str,
) -> str:
    """Rewrite XHTML chapter document, normalizing styles, attributes and viewport meta.

    The pipeline is:
    1. Snapshot five counters whose state may change.
    2. Test if the input is well-formed XML.
    3. Rewrite <style> blocks through the stylesheet normalizer.
    4. Mask HTML comments and <style> blocks to protect them from attribute rewrites.
    5. Rewrite style="" attributes through the declaration normalizer.
    6. Remove viewport meta tags.
    7. Unmask protected regions.
    8. If no counter moved, return the input verbatim (fidelity + idempotency).
    9. If the input was well-formed but the output is not, revert: restore all counters
       and return the input verbatim, logging a WARNING.
    10. Otherwise return the rewritten text.

    Args:
        text: The XHTML chapter document as a string.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes across multiple files.
        name: The chapter's manifest href, used only in revert logging (e.g., "OEBPS/c1.xhtml").

    Returns:
        The rewritten XHTML text, or the input verbatim if no changes were made or if
        the rewrite would have broken well-formedness.
    """
    # Snapshot the counters this function can move.
    snapshot = _snapshot(counts)

    # Test well-formedness before any changes.
    was_well_formed = is_well_formed(text)

    # Rewrite <style> blocks.
    working = _rewrite_style_blocks(text, options=options, counts=counts)

    # Mask comments and style blocks to protect them from attribute rewrites.
    masked, store = _mask(working)

    # Rewrite style="" attributes.
    masked = _rewrite_style_attributes(masked, options=options, counts=counts)

    # Remove viewport meta tags.
    masked = _remove_viewport_metas(masked, counts=counts)

    # Unmask protected regions.
    working = _unmask(masked, store)

    # If no counter moved, return the input verbatim.
    if snapshot == _snapshot(counts):
        return text

    # If the input was well-formed and the output is not, revert.
    if was_well_formed and not is_well_formed(working):
        _restore(counts, snapshot)
        logger.warning(
            "Normalize reverted %s: the rewrite is no longer well-formed XML; "
            "keeping the original bytes",
            name,
        )
        return text

    return working


def _snapshot(counts: NormalizeCounts) -> tuple[int, ...]:
    """Capture the five counters this function can move.

    Args:
        counts: The accumulator to snapshot.

    Returns:
        A tuple of five counter values in order.
    """
    return (
        counts.declarations_removed,
        counts.declarations_converted,
        counts.rules_removed,
        counts.style_attributes_removed,
        counts.viewport_metas_removed,
    )


def _restore(counts: NormalizeCounts, snapshot: tuple[int, ...]) -> None:
    """Restore five counters to a previously captured state.

    Args:
        counts: The accumulator to restore.
        snapshot: The tuple returned by _snapshot().
    """
    (
        counts.declarations_removed,
        counts.declarations_converted,
        counts.rules_removed,
        counts.style_attributes_removed,
        counts.viewport_metas_removed,
    ) = snapshot


def _rewrite_style_blocks(text: str, *, options: NormalizeOptions, counts: NormalizeCounts) -> str:
    """Rewrite CSS inside <style> blocks through the stylesheet normalizer.

    For each match, if the block body is wrapped in an XML comment (already inert
    markup), it is returned verbatim. Otherwise the body is normalized with
    `normalize_stylesheet`.

    Args:
        text: The XHTML text containing <style> blocks.
        options: Normalization options.
        counts: Accumulator for tracking changes.

    Returns:
        The text with <style> block contents rewritten.
    """

    def _rewrite_one(match: re.Match[str]) -> str:
        """Rewrite one <style> block or return it verbatim if it's already inert."""
        open_tag = match.group(1)
        body = match.group(2)
        close_tag = match.group(3)

        # If the body starts with an XML comment, it's already inert; keep verbatim.
        if body.lstrip().startswith("<!--"):
            return match.group(0)

        # Normalize the CSS body.
        normalized = normalize_stylesheet(body, options=options, counts=counts)
        return open_tag + normalized + close_tag

    return _STYLE_BLOCK_RE.sub(_rewrite_one, text)


def _mask(text: str) -> tuple[str, list[str]]:
    """Replace HTML comments and <style> blocks with index placeholders.

    This is what stops a `style="…"` string that merely *appears* inside a comment
    or inside CSS from being rewritten as an attribute.

    Args:
        text: The XHTML text.

    Returns:
        A tuple of (masked_text, store), where store is a list of the original
        spans in order.
    """
    store: list[str] = []

    def _swap(match: re.Match[str]) -> str:
        """Record one span and return its placeholder."""
        store.append(match.group(0))
        return f"\x00{len(store) - 1}\x00"

    return _MASK_RE.sub(_swap, text), store


def _unmask(text: str, store: list[str]) -> str:
    """Restore masked regions by index.

    Args:
        text: The masked XHTML text.
        store: The list of original spans from _mask.

    Returns:
        The text with placeholders replaced by their originals.
    """

    def _restore_one(match: re.Match[str]) -> str:
        """Restore one placeholder by index."""
        index = int(match.group(1))
        return store[index]

    return _PLACEHOLDER_RE.sub(_restore_one, text)


def _rewrite_style_attributes(
    text: str, *, options: NormalizeOptions, counts: NormalizeCounts
) -> str:
    """Rewrite style="" attributes through the declaration normalizer.

    Per match, the attribute value is parsed as a declaration list, normalized with
    an empty selector (since the element name is not knowable), and rebuilt. If it
    empties, the attribute is removed (including the whitespace before it).

    Args:
        text: The masked XHTML text.
        options: Normalization options.
        counts: Accumulator for tracking changes.

    Returns:
        The text with style attributes rewritten.
    """

    def _rewrite_one(match: re.Match[str]) -> str:
        """Rewrite one style attribute or return it unchanged if empty or identical."""
        space = match.group(1)
        quote = match.group(2)
        value = match.group(3)

        # Parse the attribute value as declarations.
        parsed = tinycss2.parse_blocks_contents(value, skip_comments=False, skip_whitespace=False)

        # Normalize with an empty selector (element name unknown).
        normalized = normalize_declarations(parsed, options=options, selector="", counts=counts)

        # Keep only declaration nodes.
        kept = [d for d in normalized if d.type == "declaration"]

        # If empty, remove the attribute and its preceding whitespace.
        if not kept:
            counts.style_attributes_removed += 1
            return ""

        # Rebuild the value.
        rebuilt = "; ".join(css_text([d]).strip().rstrip(";") for d in kept)

        # If unchanged, return the original match.
        if rebuilt == value:
            return match.group(0)

        # Otherwise return the rewritten attribute.
        return f"{space}style={quote}{rebuilt}{quote}"

    return _STYLE_ATTR_RE.sub(_rewrite_one, text)


def _remove_viewport_metas(text: str, *, counts: NormalizeCounts) -> str:
    """Remove viewport meta tags and update the counter.

    Args:
        text: The masked XHTML text.
        counts: Accumulator for tracking changes.

    Returns:
        The text with viewport meta tags removed.
    """
    result, n_removed = _VIEWPORT_META_RE.subn("", text)
    counts.viewport_metas_removed += n_removed
    return result
