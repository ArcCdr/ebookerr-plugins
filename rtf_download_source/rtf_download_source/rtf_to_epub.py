"""RTF→EPUB plugin module."""

from __future__ import annotations

import logging
from pathlib import Path

from ebookerr_sdk.epub.build import (
    Book,
    ConversionResult,
    DocumentConversionError,
    apply_title_to_untitled_chapters,
    build_epub,
)
from ebookerr_sdk.epub.text_convert import decode_imported_text, parse_text_to_book
from striprtf.striprtf import rtf_to_text

logger = logging.getLogger(__name__)


class RtfConversionError(DocumentConversionError):
    """Raised when RTF file extraction or parsing fails."""

    pass


def convert_rtf_to_epub(
    rtf_path: Path,
    epub_path: Path,
    *,
    title: str | None = None,
    author: str | None = None,
) -> ConversionResult:
    """Convert an RTF file to EPUB.

    Args:
        rtf_path: Path to the RTF file.
        epub_path: Path to write the EPUB.
        title: Optional title override. Falls back to book.title, then file stem.
        author: Optional author override.

    Returns:
        ConversionResult with the converted title, author, chapter count, first line, and
        whether the title was a filename fallback (``title_from_filename``, EXP-224).

    Raises:
        RtfConversionError: If the RTF cannot be parsed or has no text content.
    """
    raw = decode_imported_text(rtf_path.read_bytes())[0]

    try:
        text = rtf_to_text(raw)  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise RtfConversionError(f"cannot parse RTF: {exc}") from exc

    # Extract first non-empty line
    first_line: str | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    book = parse_text_to_book(text)

    if not book.chapters:
        raise RtfConversionError("no text content found in RTF")

    effective_title = title or book.title or rtf_path.stem
    effective_author = author
    title_from_filename = not (title or book.title)
    chapters = apply_title_to_untitled_chapters(book.chapters, effective_title)

    result = build_epub(
        Book(effective_title, effective_author, chapters),
        epub_path,
        cover_bytes=None,
        first_line=first_line,
        title_from_filename=title_from_filename,
    )

    if book.title is None and len(book.chapters) > 1:
        logger.info("Prose-heading split: %d chapters detected", len(book.chapters))

    if title_from_filename:
        logger.debug(
            "RTF title fell back to the filename: %r (document first line: %r)",
            effective_title,
            first_line,
        )

    logger.info(
        'Converted RTF to EPUB: "%s" (%d chapters)',
        effective_title,
        len(book.chapters),
    )

    return result
