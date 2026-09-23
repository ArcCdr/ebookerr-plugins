"""EPUB normalization orchestrator — stylesheet CSS pass, chapter XHTML pass, and font sweep.

This module walks an EPUB's members, identifies CSS stylesheets and chapter documents,
normalizes their styling while preserving structural markup, and persists changes only
when bytes actually changed (`TR-NORM-5`: a no-op run leaves the EPUB byte-identical,
preventing false `EpubModified` event cascades in the core).

**Fixed-layout bail-out** (`FR-NORM-12`): A book declaring `rendition:layout =
pre-paginated` (EPUB 3) or Apple's `<meta name="fixed-layout" content="true"/>`
is skipped entirely — comics and illustrated children's books depend on the exact
styling this plugin removes.

**Per-file markers** (`TR-NORM-3`): Each normalized file is stamped with a 12-hex
signature computed from the ruleset version and the effective options. Changing a
setting invalidates every marker automatically, so a second run with different
options re-normalizes. A file whose marker matches the current signature is skipped
(no work, no rewrite).

**Three-pass walk:**
1. CSS members (stylesheets identified by media type or .css extension).
2. Chapter members (XHTML only; SVG members are deliberately excluded per FR-NORM-10).
3. Font sweep (unreferenced files removed via `sweep_fonts()`).

CSS members are normalized via `normalize_stylesheet()`, which rewrites declarations
and removes orphaned rules and at-rules, returning its input verbatim when nothing
changed (to preserve round-trip fidelity and satisfy the byte-identical contract).
Chapter documents are normalized via `normalize_xhtml()`, which rewrites style blocks,
inline style attributes, and removes viewport meta tags while tolerating structural
defects and preserving the document byte-for-byte when no changes occur.
The font sweep runs after both passes because it decides "is anything still pointing
at this font?" by scanning the rewritten CSS and chapter text — running it earlier
would see the deleted @font-face rules and incorrectly mark fonts as unreferenced.
`EpubDocument` holds members in memory, so the sweep sees rewritten bytes without
an intermediate save.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ebookerr_sdk.domain.text_encoding import declare_utf8, decode_declared
from ebookerr_sdk.epub.document import EpubDocument
from ebookerr_sdk.epub.errors import ItemNotFoundError
from ebookerr_sdk.epub.opf import ManifestItem

from .counts import NormalizeCounts
from .fonts import sweep_fonts
from .markers import (
    read_css_marker,
    read_xhtml_marker,
    stamp_css,
    stamp_xhtml,
    strip_css_marker,
    strip_xhtml_marker,
)
from .options import NormalizeOptions, options_signature
from .stylesheet import normalize_stylesheet
from .xhtml import normalize_xhtml

logger = logging.getLogger(__name__)

FIXED_LAYOUT_REASON = "fixed-layout"


@dataclass(frozen=True, slots=True)
class NormalizeReport:
    """Result of normalizing a single EPUB.

    Attributes:
        counts: Accumulator of all changes made during normalization.
        skipped_reason: If the EPUB was skipped (not normalized), the reason
            (e.g., "fixed-layout"). None means the EPUB was processed.
    """

    counts: NormalizeCounts
    skipped_reason: str | None = None

    @property
    def changed(self) -> bool:
        """Whether the EPUB was modified.

        Returns:
            True if the EPUB was processed (not skipped) and at least one
            counter other than files_skipped_marker is non-zero.
        """
        return self.skipped_reason is None and self.counts.touched


def normalize_epub(path: Path, *, options: NormalizeOptions) -> NormalizeReport:
    """Normalize an EPUB by stripping layout-interfering styling.

    Opens the EPUB, checks for fixed-layout, normalizes CSS stylesheets,
    and persists only if bytes actually changed (the TR-NORM-5 contract).
    Exceptions from opening the archive propagate (caller decides reporting).

    Args:
        path: Filesystem path to the EPUB file.
        options: Normalization options controlling which properties to strip/convert.

    Returns:
        A NormalizeReport with counts and optional skip reason.

    Raises:
        EpubError: If the EPUB is malformed or the archive is corrupted.
        zipfile.BadZipFile: If the file is not a valid zip archive.
    """
    doc = EpubDocument.open(path)
    counts = NormalizeCounts()

    if _is_fixed_layout(doc):
        return NormalizeReport(counts, skipped_reason=FIXED_LAYOUT_REASON)

    signature = options_signature(options)
    _normalize_css_members(doc, options=options, counts=counts, signature=signature)
    _normalize_xhtml_members(doc, options=options, counts=counts, signature=signature)

    # Font sweep runs after CSS and XHTML rewrites so it sees the updated members
    # without deleted @font-face rules, accurately identifying unreferenced fonts.
    if options.strip_fonts:
        sweep_fonts(doc, counts=counts)

    if counts.touched:
        doc.save()

    logger.debug(
        "Normalize finished %s: %d css, %d xhtml, %d font(s) changed; %d skipped by marker",
        path.name,
        counts.css_files_changed,
        counts.xhtml_files_changed,
        counts.font_files_removed,
        counts.files_skipped_marker,
    )

    return NormalizeReport(counts)


def _is_fixed_layout(doc: EpubDocument) -> bool:
    """Check whether the EPUB is marked as fixed-layout.

    Returns True if the OPF declares `fixed-layout="true"` or contains
    `rendition:layout = pre-paginated`. A false positive (incorrectly identifying
    a reflowable book as fixed) is the safe direction: the book is skipped and
    untouched.

    Args:
        doc: The open EPUB document.

    Returns:
        True if the book is fixed-layout; False otherwise.
    """
    if (doc.opf.get_meta_content("fixed-layout") or "").strip().lower() == "true":
        return True
    return "pre-paginated" in doc.opf.to_xml().lower()


def _css_members(doc: EpubDocument) -> list[ManifestItem]:
    """Return every manifest item that is or should be a CSS stylesheet.

    Detects CSS by media type (`text/css`) or by file extension (`.css`),
    because some publishers mislabel stylesheets.

    Args:
        doc: The open EPUB document.

    Returns:
        A list of ManifestItem objects that are CSS stylesheets.
    """
    items = []
    for item in doc.opf.manifest():
        if item.media_type.lower() == "text/css" or item.href.lower().endswith(".css"):
            items.append(item)
    return items


def _normalize_css_members(
    doc: EpubDocument,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    signature: str,
) -> None:
    """Normalize every CSS stylesheet in the EPUB.

    For each stylesheet: read its bytes, decode strictly in its declared encoding
    (``TXE-D8``; a member that cannot be decoded that way is left completely
    untouched, WARNING logged), check for idempotency marker (skip if matched),
    normalize declarations via `normalize_stylesheet()`, and write only if bytes
    changed — redeclared as UTF-8. Missing members (manifest row without archive
    entry) are tolerated and logged at DEBUG level.

    Modifies `doc` and `counts` in place.

    Args:
        doc: The open EPUB document.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.
        signature: The 12-hex options_signature to stamp on changed files.
    """
    for item in _css_members(doc):
        try:
            raw = doc.resource_bytes(item.href)
        except (ItemNotFoundError, KeyError):
            logger.debug("Normalize skipped missing stylesheet %s", item.href)
            continue

        decoded = decode_declared(raw, kind="css")
        if decoded is None:
            logger.warning(
                "Normalize skipped %s: not decodable in its declared encoding", item.href
            )
            continue
        text, _enc = decoded

        if read_css_marker(text) == signature:
            counts.files_skipped_marker += 1
            logger.debug(
                "Normalize skipped %s: marker matches signature %s",
                item.href,
                signature,
            )
            continue

        body = strip_css_marker(text)
        new_body = normalize_stylesheet(body, options=options, counts=counts)

        if new_body == body:
            continue

        stamped = stamp_css(new_body, signature)
        doc.write_resource(item.href, declare_utf8(stamped, kind="css").encode("utf-8"))
        counts.css_files_changed += 1
        logger.debug("Normalize rewrote stylesheet %s", item.href)


_XHTML_SUFFIXES = (".xhtml", ".html", ".htm")


def _xhtml_members(doc: EpubDocument) -> list[ManifestItem]:
    """Return every manifest item that is or should be an XHTML chapter document.

    Detects XHTML by media type (`application/xhtml+xml`) or by file extension
    (`.xhtml`, `.html`, `.htm`), because some publishers mislabel chapter files.
    SVG members are deliberately excluded to preserve embedded SVG title pages
    per FR-NORM-10.

    Args:
        doc: The open EPUB document.

    Returns:
        A list of ManifestItem objects that are XHTML chapter documents.
    """
    items = []
    for item in doc.opf.manifest():
        # Exclude SVG members
        if item.media_type.lower() == "image/svg+xml":
            continue

        if item.media_type.lower() == "application/xhtml+xml" or item.href.lower().endswith(
            _XHTML_SUFFIXES
        ):
            items.append(item)
    return items


def _normalize_xhtml_members(
    doc: EpubDocument,
    *,
    options: NormalizeOptions,
    counts: NormalizeCounts,
    signature: str,
) -> None:
    """Normalize every XHTML chapter document in the EPUB.

    For each chapter: read its bytes, decode strictly in its declared encoding
    (``TXE-D8``; a member that cannot be decoded that way is left completely
    untouched, WARNING logged), check for idempotency marker (skip if matched),
    normalize styles via `normalize_xhtml()`, and write only if bytes changed —
    redeclared as UTF-8. Missing members (manifest row without archive entry) are
    tolerated and logged at DEBUG level.

    Modifies `doc` and `counts` in place.

    Args:
        doc: The open EPUB document.
        options: Normalization options controlling which properties to strip/convert.
        counts: Accumulator for tracking changes.
        signature: The 12-hex options_signature to stamp on changed files.
    """
    for item in _xhtml_members(doc):
        try:
            raw = doc.resource_bytes(item.href)
        except (ItemNotFoundError, KeyError):
            logger.debug("Normalize skipped missing chapter %s", item.href)
            continue

        decoded = decode_declared(raw, kind="xml")
        if decoded is None:
            logger.warning(
                "Normalize skipped %s: not decodable in its declared encoding", item.href
            )
            continue
        text, _enc = decoded

        if read_xhtml_marker(text) == signature:
            counts.files_skipped_marker += 1
            logger.debug(
                "Normalize skipped %s: marker matches signature %s",
                item.href,
                signature,
            )
            continue

        body = strip_xhtml_marker(text)
        new_body = normalize_xhtml(body, options=options, counts=counts, name=item.href)

        if new_body == body:
            continue

        stamped = stamp_xhtml(new_body, signature)
        doc.write_resource(item.href, declare_utf8(stamped, kind="xml").encode("utf-8"))
        counts.xhtml_files_changed += 1
        logger.debug("Normalize rewrote chapter %s", item.href)
