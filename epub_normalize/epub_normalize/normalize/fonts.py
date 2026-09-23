"""Removal of unreferenced embedded fonts during EPUB normalization.

This module removes font files from an EPUB that are no longer referenced by any
document in the book. The core policy is simple: "is anything still pointing at it?"

When font-family declarations and @font-face rules are stripped (by another pass),
the embedded font files they referenced become dead weight — commonly 200 KB to 2 MB
per book. This module cleans them up.

The reference search uses a plain substring scan over the lowercased content of every
CSS, XHTML, HTML, SVG member. This approach is conservative: a false *positive* keeps
a font (safe direction), while a font referenced from anywhere we can read is never
deleted. The OPF and NCX are deliberately excluded — the OPF's own manifest item is
the reference we are about to delete, and no reader resolves a font through the NCX.

Font items are detected by media type (FONT_MEDIA_TYPES from rules.py) or file
extension (FONT_EXTENSIONS), so both detection methods act together.

When unreferenced fonts are removed, their entries in META-INF/encryption.xml
(Adobe/IDPF obfuscation, MR-FONT-1) are also pruned via text edit so that orphaned
encryption metadata does not accumulate. The full encryption.xml is deleted only when
no encrypted resources remain.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from ebookerr_sdk.domain.text_encoding import declare_utf8, decode_declared
from ebookerr_sdk.epub.document import EpubDocument

from .counts import NormalizeCounts
from .rules import FONT_EXTENSIONS, FONT_MEDIA_TYPES

logger = logging.getLogger(__name__)

_ENCRYPTED_DATA_RE = re.compile(
    r"<(?:\w+:)?EncryptedData\b.*?</(?:\w+:)?EncryptedData>",
    re.DOTALL | re.IGNORECASE,
)


def sweep_fonts(doc: EpubDocument, *, counts: NormalizeCounts) -> None:
    """Remove unreferenced embedded font files from the EPUB and clean encryption metadata.

    Args:
        doc: The EPUB document to sweep.
        counts: Accumulator for change statistics; incremented by the number of
            font files removed.
    """
    # Step 1: Collect font manifest items.
    font_items = [
        item
        for item in doc.opf.manifest()
        if item.media_type.lower() in FONT_MEDIA_TYPES
        or PurePosixPath(item.href).suffix.lower() in FONT_EXTENSIONS
    ]
    if not font_items:
        return

    # Step 2: Build the reference corpus.
    corpus = _reference_corpus(doc)

    # Step 3: Identify and remove unreferenced fonts.
    removed_hrefs: list[str] = []
    for item in font_items:
        basename = PurePosixPath(item.href).name.lower()
        if basename not in corpus:
            removed_hrefs.append(item.href)
            doc.remove_manifest_resource(item.id)
            counts.font_files_removed += 1
            logger.debug(
                "Normalize removed unreferenced font %s (item_id=%s)",
                item.href,
                item.id,
            )

    # Step 4: If nothing was removed, exit.
    if not removed_hrefs:
        return

    # Step 5: Prune encryption.xml.
    _prune_encryption(doc, removed_hrefs)


def _reference_corpus(doc: EpubDocument) -> str:
    """Build a lowercased string corpus of all readable CSS, XHTML, HTML, SVG content.

    The corpus includes every member whose lowercased suffix is .css, .xhtml, .html,
    .htm, or .svg. Members whose bytes are None are skipped (no-op when a member is
    missing). The OPF and NCX are deliberately excluded.

    Args:
        doc: The EPUB document.

    Returns:
        A single lowercased string containing all readable CSS/XHTML/SVG content.
    """
    content_parts: list[str] = []
    readables = {".css", ".xhtml", ".html", ".htm", ".svg"}

    for name in doc.member_names():
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in readables:
            continue
        raw_bytes = doc.member_bytes(name)
        if raw_bytes is None:
            continue
        content = raw_bytes.decode("utf-8", errors="replace")
        content_parts.append(content.lower())

    return "".join(content_parts)


def _prune_encryption(doc: EpubDocument, removed_hrefs: list[str]) -> None:
    """Remove encryption.xml entries for the given removed font hrefs.

    Decodes encryption.xml strictly in its declared encoding (``TXE-D8``); if it cannot
    be decoded that way, it is left completely untouched (WARNING logged) rather than
    risking corruption. Otherwise drops every <EncryptedData> block that mentions any of
    the removed hrefs. If encrypted resources remain, writes the pruned XML back,
    redeclared as UTF-8. If none remain, deletes the encryption.xml member.

    Args:
        doc: The EPUB document.
        removed_hrefs: List of removed font hrefs to match against encryption entries.
    """
    raw = doc.member_bytes("META-INF/encryption.xml")
    if raw is None:
        return

    decoded = decode_declared(raw, kind="xml")
    if decoded is None:
        logger.warning(
            "Normalize left encryption.xml untouched: not decodable in its declared encoding"
        )
        return
    text, _enc = decoded

    # Find all EncryptedData blocks and drop those mentioning removed hrefs.
    dropped = 0

    def should_drop(block: re.Match[str]) -> bool:
        """Check if an EncryptedData block references a removed font."""
        block_text = block.group(0)
        for href in removed_hrefs:
            # Compare against both the full href and its basename.
            if href in block_text or PurePosixPath(href).name in block_text:
                return True
        return False

    def replacer(match: re.Match[str]) -> str:
        """Drop EncryptedData block if it references a removed font, else keep it."""
        nonlocal dropped
        if should_drop(match):
            dropped += 1
            return ""
        return match.group(0)

    pruned = _ENCRYPTED_DATA_RE.sub(replacer, text)

    # If encrypted entries remain, write the pruned XML back.
    if "<encrypteddata" in pruned.lower():
        doc.write_member(
            "META-INF/encryption.xml", declare_utf8(pruned, kind="xml").encode("utf-8")
        )
        logger.debug("Normalize pruned %d encryption entry/entries", dropped)
    else:
        # Otherwise, delete the empty encryption.xml.
        doc.remove_member("META-INF/encryption.xml")
        logger.debug("Normalize removed META-INF/encryption.xml (no encrypted resources left)")
