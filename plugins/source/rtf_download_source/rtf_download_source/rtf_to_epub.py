r"""RTF→EPUB plugin module.

Converts an RTF file to EPUB: ``\info`` names the book (EXP-302), every ``\par`` is a
paragraph break, the shared text parser finds the chapters, and a file the converter cannot use
raises :class:`RtfRefusedError` with one sentence (EXP-297).
"""

from __future__ import annotations

import logging
import re
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

RTF_DAMAGED = "This RTF file is damaged and can't be converted."
RTF_NO_TEXT = "This RTF file has no text to convert."

_INFO_RES = {
    "title": re.compile(r"\{\\title\s+((?:[^{}\\]|\\.)*)\}"),
    "author": re.compile(r"\{\\author\s+((?:[^{}\\]|\\.)*)\}"),
}


class RtfConversionError(DocumentConversionError):
    """Raised when RTF file extraction or parsing fails."""

    pass


class RtfRefusedError(RtfConversionError):
    """An RTF file this converter cannot use, for a cause the user can act on (EXP-297).

    Its message is one fixed sentence; ``user_facing`` tells the SDK stage to show it as is.
    """

    user_facing = True


def _info_text(raw: str, field: str) -> str | None:
    r"""Return the text of the ``{\info{\<field> …}}`` group of *raw*, decoded, or ``None``.

    Args:
        raw: The RTF source, decoded.
        field: ``"title"`` or ``"author"``.

    Returns:
        The field's text with RTF escapes resolved, or ``None`` when absent or empty.
    """
    match = _INFO_RES[field].search(raw)
    if match is None:
        return None
    value = rtf_to_text("{\\rtf1 " + match.group(1) + "}").strip()  # type: ignore[no-untyped-call]
    return value or None


def convert_rtf_to_epub(
    rtf_path: Path,
    epub_path: Path,
    *,
    title: str | None = None,
    author: str | None = None,
) -> ConversionResult:
    r"""Convert an RTF file to EPUB.

    Args:
        rtf_path: Path to the RTF file.
        epub_path: Path to write the EPUB.
        title: Optional title override. Falls back to ``\info\title``, the parsed title,
            then the file stem.
        author: Optional author override. Falls back to ``\info\author``.

    Returns:
        ConversionResult with the converted title, author, chapter count, first line, and
        whether the title was a filename fallback (``title_from_filename``, EXP-224).

    Raises:
        RtfRefusedError: The RTF cannot be parsed, or holds no text.
    """
    raw = decode_imported_text(rtf_path.read_bytes())[0]

    try:
        text = rtf_to_text(raw)  # type: ignore[no-untyped-call]
    except Exception as exc:
        logger.debug("RTF %s could not be parsed: %s", rtf_path.name, exc)
        raise RtfRefusedError(RTF_DAMAGED) from exc

    info_title = _info_text(raw, "title")
    info_author = _info_text(raw, "author")

    # Extract first non-empty line
    first_line: str | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    # Every \par (one newline in striprtf's output) is a paragraph break (EXP-302).
    book = parse_text_to_book("\n\n".join(text.split("\n")))

    if not book.chapters:
        raise RtfRefusedError(RTF_NO_TEXT)

    effective_title = title or info_title or book.title or rtf_path.stem
    effective_author = author or info_author
    title_from_filename = not (title or info_title or book.title)
    chapters = apply_title_to_untitled_chapters(book.chapters, effective_title)

    result = build_epub(
        Book(effective_title, effective_author, chapters, book.front_matter),
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
