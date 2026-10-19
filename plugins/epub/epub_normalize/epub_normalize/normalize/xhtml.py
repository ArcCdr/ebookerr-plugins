"""Surgical rewriting of XHTML chapter documents (style blocks, start tags, viewport meta).

This module rewrites chapter documents by targeted text substitution over masked source,
avoiding XML parsing altogether (`TR-NORM-1`). This is critical for two reasons:

1. **Tolerance of structural defects**: stdlib `ElementTree` silently drops the DOCTYPE,
   comments and processing instructions on round-trip, and refuses outright to parse a chapter
   with a structural defect (e.g., mismatched tags) or an HTML entity such as `&nbsp;`. Real EPUB
   books commonly have both, so rewriting the raw text ensures we tolerate what readers must
   tolerate.

2. **The masking guard** — before touching start tags, we mask out every HTML comment and every
   inline `<svg>`, `<math>` and `<script>` subtree, then every `<style>` block, with index
   placeholders held in one shared store. A `style="…"` that merely *appears* inside a comment,
   a script or CSS is never rewritten, and the content of an inline vector graphic (`FR-NORM-2`)
   is never changed. The order matters: mask, rewrite, then unmask.

**Start tags judged by their own element** — each start tag is parsed on its own: its `style`
attribute is normalised under the `RuleContext` its tag, classes and id give (against what the
survey learned about the book), and the HTML presentational attributes the level removes are cut
(`FR-NORM-20`). A start tag nothing changes is left byte-identical.

**Well-formedness as a relative guard** (`TR-NORM-2`): The before/after well-formedness probe is
a *relative* check, not an absolute validity claim. A chapter that was well-formed XML before the
rewrite but is not after has had a genuine regression — it must be reverted. A chapter that
already failed to parse (e.g., because of `&nbsp;` or mismatched tags) is rewritten without
re-parsing, because the regression guard only fires on a *change in status*.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import tinycss2
from ebookerr_sdk.epub.errors import MalformedEpubError
from ebookerr_sdk.epub.safe_xml import safe_fromstring

from .attributes import strip_presentational_attributes
from .counts import NormalizeCounts
from .declarations import (
    css_text,
    normalize_declarations,
)
from .options import NormalizeOptions
from .selectors import RuleContext, context_of, element_selector
from .stylesheet import normalize_stylesheet
from .survey import ATTR_RE, START_TAG_RE, BookContext

logger = logging.getLogger(__name__)

_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL)
_PROTECTED_RE = re.compile(
    r"<!--.*?-->"
    r"|<(?:\w+:)?svg\b[^>]*>.*?</(?:\w+:)?svg\s*>"
    r"|<(?:\w+:)?math\b[^>]*>.*?</(?:\w+:)?math\s*>"
    r"|<script\b[^>]*>.*?</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")
_VIEWPORT_META_RE = re.compile(
    r"[ \t]*<meta\b[^>]*\bname\s*=\s*[\"']viewport[\"'][^>]*>[ \t]*\n?",
    re.IGNORECASE,
)
_PAGE_TEMPLATE_LINK_RE = re.compile(
    r"[ \t]*<link\b[^>]*(?:vnd\.adobe-page-template\+xml|\.xpgt[\"'])[^>]*>[ \t]*\n?",
    re.IGNORECASE,
)
_EMPTY_INLINE_RE = re.compile(
    r"</?(?:span|b|i|em|strong|u|s|small|big|sub|sup|font|a)\b[^>]*>", re.IGNORECASE
)
_SELF_CLOSING_P_RE = re.compile(r"<p\b([^>]*?)\s*/>", re.IGNORECASE)
_PAIRED_P_RE = re.compile(r"(<p\b(?![^>]*/>)[^>]*>)(.*?)(</p\s*>)", re.IGNORECASE | re.DOTALL)


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
    book: BookContext | None = None,
    blank_lines: bool = False,
) -> str:
    """Rewrite XHTML chapter document, normalizing styles, attributes and blank lines.

    The pipeline is:
    1. Snapshot the counters this function can move.
    2. Test if the input is well-formed XML.
    3. Mask comments and inline ``<svg>``, ``<math>`` and ``<script>`` subtrees.
    4. Rewrite <style> blocks through the stylesheet normalizer.
    5. Mask the remaining <style> blocks into the same store.
    6. Rewrite every start tag: its style="" attribute under the context of its own element,
       and the HTML presentational attributes the level removes.
    7. Remove viewport meta tags when the level strips fixed dimensions.
    8. Remove Adobe page-template links when the level strips the page layout.
    9. Mark empty paragraphs with a non-breaking space when *blank_lines* is set.
    10. Unmask protected regions, twice: a style block may hold a placeholder of step 3.
    11. If no counter moved, return the input verbatim (fidelity + idempotency).
    12. If the input was well-formed but the output is not, revert: restore all counters
        and return the input verbatim, logging a WARNING.
    13. Otherwise return the rewritten text.

    Args:
        text: The XHTML chapter document as a string.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes across multiple files.
        name: The chapter's manifest href, used only in revert logging (e.g., "OEBPS/c1.xhtml").
        book: What the survey learned about the book; an empty ``BookContext`` when omitted.
        blank_lines: Whether empty paragraphs get a non-breaking space (FR-NORM-18).

    Returns:
        The rewritten XHTML text, or the input verbatim if no changes were made or if
        the rewrite would have broken well-formedness.
    """
    book = BookContext() if book is None else book
    # Snapshot the counters this function can move.
    snapshot = _snapshot(counts)

    # Test well-formedness before any changes.
    was_well_formed = is_well_formed(text)

    # Mask comments and inline vector, math and script subtrees; they are never changed.
    store: list[str] = []
    masked = _mask(text, _PROTECTED_RE, store)

    # Rewrite <style> blocks, then mask them so no start-tag rewrite can reach into CSS.
    masked = _rewrite_style_blocks(masked, options=options, counts=counts, book=book, store=store)
    masked = _mask(masked, _STYLE_BLOCK_RE, store)

    # Rewrite the style="" and presentational attributes of every start tag.
    masked = _rewrite_start_tags(masked, options=options, book=book, counts=counts)

    # Remove viewport meta tags only where the level fixes dimensions.
    if options.strip_fixed_dimensions:
        masked = _remove_viewport_metas(masked, counts=counts)

    # Remove Adobe page-template links only where the level strips the page layout.
    if options.strip_page_layout:
        masked = _remove_page_template_links(masked, counts=counts)

    # Put a non-breaking space into every empty paragraph, so a blank line still shows.
    if blank_lines:
        masked, marked = mark_blank_lines(masked)
        counts.blank_lines_marked += marked

    # Unmask twice: a style block masked in step 5 may hold a placeholder of step 3.
    working = _unmask(_unmask(masked, store), store)

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
    """Capture the counters this function can move.

    Args:
        counts: The accumulator to snapshot.

    Returns:
        A tuple of the counter values in order.
    """
    return (
        counts.declarations_removed,
        counts.declarations_converted,
        counts.rules_removed,
        counts.style_attributes_removed,
        counts.viewport_metas_removed,
        counts.html_attributes_removed,
        counts.blank_lines_marked,
        counts.page_template_links_removed,
    )


def _restore(counts: NormalizeCounts, snapshot: tuple[int, ...]) -> None:
    """Restore the counters to a previously captured state.

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
        counts.html_attributes_removed,
        counts.blank_lines_marked,
        counts.page_template_links_removed,
    ) = snapshot


def _rewrite_style_blocks(
    text: str,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    book: BookContext,
    store: list[str],
) -> str:
    """Rewrite CSS inside <style> blocks through the stylesheet normalizer.

    For each match, the block body is first restored from *store* (a placeholder holds a NUL
    character, which the CSS parser would replace). If the body is wrapped in an XML comment
    (already inert markup), the block is returned verbatim. Otherwise the body is normalized
    with `normalize_stylesheet`.

    Args:
        text: The masked XHTML text containing <style> blocks.
        options: Normalization options.
        counts: Accumulator for tracking changes.
        book: What the survey learned about the book.
        store: The masked spans, to restore a comment held inside a block's body.

    Returns:
        The text with <style> block contents rewritten.
    """

    def _rewrite_one(match: re.Match[str]) -> str:
        """Rewrite one <style> block or return it verbatim if it's already inert."""
        open_tag = match.group(1)
        body = _unmask(match.group(2), store)
        close_tag = match.group(3)

        # If the body starts with an XML comment, it's already inert; keep verbatim.
        if body.lstrip().startswith("<!--"):
            return open_tag + body + close_tag

        # Normalize the CSS body.
        normalized = normalize_stylesheet(body, options=options, counts=counts, book=book)
        return open_tag + normalized + close_tag

    return _STYLE_BLOCK_RE.sub(_rewrite_one, text)


def _mask(text: str, pattern: re.Pattern[str], store: list[str]) -> str:
    r"""Replace every match of *pattern* with an index placeholder.

    This is what stops a `style="…"` string that merely *appears* inside a comment, a script or
    CSS from being rewritten as an attribute, and keeps inline vector content out of reach.

    Args:
        text: The XHTML text.
        pattern: The spans to protect.
        store: The list of original spans; matches are appended, so an index stays valid
            across several calls that share one store.

    Returns:
        The text with each span replaced by ``\x00<index>\x00``.
    """

    def _swap(match: re.Match[str]) -> str:
        """Record one span and return its placeholder."""
        store.append(match.group(0))
        return f"\x00{len(store) - 1}\x00"

    return pattern.sub(_swap, text)


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


def _rewrite_start_tags(
    text: str, *, options: NormalizeOptions, book: BookContext, counts: NormalizeCounts
) -> str:
    """Rewrite the style="" and presentational attributes of every start tag.

    Args:
        text: The masked XHTML text.
        options: Normalization options.
        book: What the survey learned about the book.
        counts: Accumulator for tracking changes.

    Returns:
        The text with each start tag rewritten; a tag nothing changes is byte-identical.
    """
    return START_TAG_RE.sub(
        lambda m: _rewrite_tag(m, options=options, book=book, counts=counts), text
    )


def _attribute_segments(attr_text: str) -> list[tuple[str, str, str | None]]:
    """Split a start tag's attribute text into (segment, lower-case name, raw value) triples."""
    return [(m.group(0), m.group(2).lower(), m.group(4)) for m in ATTR_RE.finditer(attr_text)]


def _style_segment(
    segment: str,
    raw: str | None,
    *,
    context: RuleContext,
    options: NormalizeOptions,
    book: BookContext,
    counts: NormalizeCounts,
) -> str | None:
    """Return the normalised style attribute segment, the original, or None when it empties.

    Per segment, the quoted value is parsed as a declaration list, normalized under *context*
    and rebuilt. If it empties, the segment is dropped (its leading whitespace with it). An
    unquoted value is not a style attribute XHTML can carry and is left alone.

    Args:
        segment: The attribute with its leading whitespace, as written.
        raw: The raw value with its quotes, or None for a bare attribute.
        context: How the rules treat the element the attribute sits on.
        options: Normalization options.
        book: What the survey learned about the book.
        counts: Accumulator for tracking changes.

    Returns:
        The segment to write, or None when the attribute is removed.
    """
    if raw is None or raw[0] not in "\"'":
        return segment
    quote, value = raw[0], raw[1:-1]

    # Parse the attribute value as declarations and normalize them under the element's context.
    moved_before = (counts.declarations_removed, counts.declarations_converted)
    parsed = tinycss2.parse_blocks_contents(value, skip_comments=False, skip_whitespace=False)
    normalized = normalize_declarations(
        parsed, options=options, context=context, book=book, counts=counts
    )

    # Keep only declaration nodes.
    kept = [d for d in normalized if d.type == "declaration"]

    # If empty, remove the attribute and its preceding whitespace.
    if not kept:
        counts.style_attributes_removed += 1
        return None

    # Keep the original text when no declaration was removed or converted.
    if moved_before == (counts.declarations_removed, counts.declarations_converted):
        return segment

    # Otherwise rebuild the value from the kept declarations.
    rebuilt = "; ".join(css_text([d]).strip().rstrip(";") for d in kept)
    space = segment[: len(segment) - len(segment.lstrip())]
    return f"{space}style={quote}{rebuilt}{quote}"


def _rewrite_tag(
    match: re.Match[str], *, options: NormalizeOptions, book: BookContext, counts: NormalizeCounts
) -> str:
    """Rewrite one start tag, or return it as written when none of its attributes change.

    The tag's classes and id fix its context; the presentational attributes the level removes
    are cut with their leading whitespace, and the style attribute is normalised in place.

    Args:
        match: A ``START_TAG_RE`` match.
        options: Normalization options.
        book: What the survey learned about the book.
        counts: Accumulator for tracking changes.

    Returns:
        The rewritten start tag.
    """
    tag, attr_text = match.group(1), match.group(2)
    segments = _attribute_segments(attr_text)
    values = {name: raw.strip("\"'") for _, name, raw in segments if raw is not None}
    context = context_of(element_selector(tag, values.get("class"), values.get("id")), book)

    # The survivors come back in order, so walk them alongside the segments.
    survivors = iter(
        strip_presentational_attributes(
            tag.lower(), [(name, raw) for _, name, raw in segments], options=options
        )
    )
    wanted = next(survivors, None)
    kept: list[str] = []
    for segment, name, raw in segments:
        if (name, raw) != wanted:
            counts.html_attributes_removed += 1
            continue
        wanted = next(survivors, None)
        if name == "style":
            new_segment = _style_segment(
                segment, raw, context=context, options=options, book=book, counts=counts
            )
            if new_segment is None:
                continue
            segment = new_segment
        kept.append(segment)

    rebuilt = "".join(kept)
    if rebuilt == attr_text:
        return match.group(0)
    return "<" + tag + rebuilt + match.group(0)[match.end(2) - match.start(0) :]


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


def _remove_page_template_links(text: str, *, counts: NormalizeCounts) -> str:
    """Remove Adobe page-template links and update the counter.

    Args:
        text: The masked XHTML text.
        counts: Accumulator for tracking changes.

    Returns:
        The text with page-template links removed.
    """
    result, n_removed = _PAGE_TEMPLATE_LINK_RE.subn("", text)
    counts.page_template_links_removed += n_removed
    return result


def mark_blank_lines(text: str) -> tuple[str, int]:
    r"""Put one ``&#160;`` into every empty paragraph; return the text and how many (FR-NORM-18).

    A self-closing ``<p …/>`` becomes ``<p …>&#160;</p>``; a paired ``<p …>…</p>`` whose inner
    text is empty once ``_EMPTY_INLINE_RE`` matches are removed and ASCII whitespace
    (" \t\r\n") is stripped gets ``&#160;`` right before its ``</p>``. A masked placeholder,
    ``<br/>``, an image or any text keeps a paragraph non-empty.

    Args:
        text: A chapter's masked XHTML.

    Returns:
        The text with every empty paragraph marked, and how many were marked.
    """
    marked = 0

    def _self_closing(match: re.Match[str]) -> str:
        """Expand one self-closing paragraph into an empty paired one holding a space."""
        nonlocal marked
        marked += 1
        return f"<p{match.group(1)}>&#160;</p>"

    def _paired(match: re.Match[str]) -> str:
        """Add a space before the closing tag of a paragraph that is empty."""
        nonlocal marked
        inner = _EMPTY_INLINE_RE.sub("", match.group(2)).strip(" \t\r\n")
        if inner:
            return match.group(0)
        marked += 1
        return match.group(1) + match.group(2) + "&#160;" + match.group(3)

    text = _SELF_CLOSING_P_RE.sub(_self_closing, text)
    text = _PAIRED_P_RE.sub(_paired, text)
    return text, marked
