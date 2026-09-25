"""python-docx-based DOCX text/chapter extraction."""

import logging
from pathlib import Path

import docx
from ebookerr_sdk.epub.build import (
    Book,
    Chapter,
    ConversionResult,
    DocumentConversionError,
    apply_title_to_untitled_chapters,
    build_epub,
)

logger = logging.getLogger(__name__)


class DocxConversionError(DocumentConversionError):
    """Raised when DOCX extraction fails."""

    pass


def extract_docx_book(docx_path: Path) -> Book:
    """Extract text and chapters from a DOCX file.

    Args:
        docx_path: Path to the DOCX file.

    Returns:
        Book with metadata, chapters, and paragraph text.

    Raises:
        DocxConversionError: If the DOCX cannot be opened or has no text content.
    """
    try:
        document = docx.Document(str(docx_path))
    except Exception as exc:
        raise DocxConversionError(f"cannot open DOCX: {exc}") from exc

    # Extract metadata
    title = document.core_properties.title or None
    author = document.core_properties.author or None

    # Separate headings and body paragraphs
    chapters_list: list[tuple[str, list[str], bool]] = []
    current_chapter_title: str | None = None
    current_chapter_has_heading = False
    current_chapter_paras: list[str] = []

    # Process all paragraphs
    for para in document.paragraphs:
        para_text = para.text.strip()

        # Skip empty paragraphs
        if not para_text:
            continue

        # Check if this is a heading
        style_name = (para.style.name if para.style else None) or ""
        is_heading = style_name.startswith("Heading") or style_name == "Title"

        if is_heading:
            # Save current chapter if it has content
            if current_chapter_title is not None and current_chapter_paras:
                chapters_list.append(
                    (current_chapter_title, current_chapter_paras, current_chapter_has_heading)
                )

            # Start new chapter
            current_chapter_title = para_text
            current_chapter_has_heading = True
            current_chapter_paras = []
        else:
            # Body paragraph
            if current_chapter_title is None:
                # Body text before any heading: no natural chapter title
                current_chapter_title = ""
                current_chapter_has_heading = False

            current_chapter_paras.append(para_text)

    # Save final chapter if it has content
    if current_chapter_title is not None and current_chapter_paras:
        chapters_list.append(
            (current_chapter_title, current_chapter_paras, current_chapter_has_heading)
        )

    # Check that we have at least some content
    if not chapters_list:
        raise DocxConversionError("no text content found in DOCX")

    # Build Chapter objects
    chapters = tuple(
        Chapter(chapter_title, tuple(paras), has_heading)
        for chapter_title, paras, has_heading in chapters_list
    )

    return Book(title, author, chapters)


def _extract_first_paragraph(docx_path: Path) -> str | None:
    """Extract the first non-empty paragraph text from a DOCX file.

    Args:
        docx_path: Path to the DOCX file.

    Returns:
        The first non-empty paragraph text, or None if no paragraphs found.
    """
    try:
        document = docx.Document(str(docx_path))
        for para in document.paragraphs:
            para_text = para.text.strip()
            if para_text:
                return para_text
        return None
    except Exception as exc:
        logger.debug("First paragraph extraction failed for %s: %s", docx_path.name, exc)
        return None


def _extract_docx_cover(docx_path: Path) -> bytes | None:
    """Extract the first image from a DOCX as cover bytes.

    Args:
        docx_path: Path to the DOCX file.

    Returns:
        PNG bytes of the first image, or None if no image found.
    """
    try:
        document = docx.Document(str(docx_path))

        # Iterate through relationships looking for images
        for rel in document.part.rels.values():
            if "image" in rel.reltype:
                return rel.target_part.blob  # type: ignore[no-any-return]

        return None
    except Exception as exc:
        logger.debug("DOCX cover extraction skipped: %s", exc)
        return None


def convert_docx_to_epub(
    docx_path: Path, epub_path: Path, *, title: str | None = None, author: str | None = None
) -> ConversionResult:
    """Convert a DOCX to EPUB with metadata and optional cover image.

    Args:
        docx_path: Path to the DOCX file.
        epub_path: Path to write the EPUB file.
        title: Override the DOCX's title (defaults to DOCX title or filename stem).
        author: Override the DOCX's author.

    Returns:
        ConversionResult with the converted title, author, chapter count, first line, and
        whether the title was a filename fallback (``title_from_filename``, EXP-224).

    Raises:
        DocxConversionError: If the DOCX cannot be converted or has no text content.
    """
    book = extract_docx_book(docx_path)

    # Effective title and author
    effective_title = title or book.title or docx_path.stem
    effective_author = author or book.author
    chapters = apply_title_to_untitled_chapters(book.chapters, effective_title)
    title_from_filename = not (title or book.title)

    # Extract cover image and first paragraph
    cover = _extract_docx_cover(docx_path)
    first_line = _extract_first_paragraph(docx_path)

    result = build_epub(
        Book(title=effective_title, author=effective_author, chapters=chapters),
        epub_path,
        cover_bytes=cover,
        first_line=first_line,
        title_from_filename=title_from_filename,
    )

    if title_from_filename:
        logger.debug(
            "DOCX title fell back to the filename: %r (document first line: %r)",
            effective_title,
            first_line,
        )

    logger.info(
        'Converted DOCX to EPUB: "%s" (%d chapters, cover=%s)',
        effective_title,
        len(book.chapters),
        bool(cover),
    )

    return result
