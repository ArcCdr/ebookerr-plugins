"""PyMuPDF-based PDF text/chapter extraction."""

import logging
import statistics
from pathlib import Path

import pymupdf
from ebookerr_sdk.epub.build import (
    Book,
    Chapter,
    ConversionResult,
    DocumentConversionError,
    apply_title_to_untitled_chapters,
    build_epub,
)

logger = logging.getLogger(__name__)


class PdfConversionError(DocumentConversionError):
    """Raised when PDF extraction fails."""

    pass


def extract_pdf_book(pdf_path: Path) -> Book:
    """Extract text and chapters from a PDF file.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Book with metadata, chapters, and paragraph text.

    Raises:
        PdfConversionError: If the PDF cannot be opened or parsed.
    """
    try:
        doc = pymupdf.open(pdf_path)  # type: ignore[no-untyped-call]
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise PdfConversionError(f"cannot open PDF: {exc}") from exc

    try:
        # Extract metadata
        title = (doc.metadata or {}).get("title") or None
        author = (doc.metadata or {}).get("author") or None

        # Get TOC and extract chapters
        toc = doc.get_toc(simple=True)

        if toc:
            chapters = _extract_chapters_from_toc(doc, toc)
        else:
            chapters = _extract_chapters_from_heuristic(doc)

        logger.debug(
            "PDF extracted: %d pages, %d chapters (toc=%s)",
            doc.page_count,
            len(chapters),
            bool(toc),
        )

        return Book(title, author, chapters)
    finally:
        doc.close()  # type: ignore[no-untyped-call]


def _extract_chapters_from_toc(
    doc: pymupdf.Document,
    toc: list[list],  # type: ignore[type-arg]
) -> tuple[Chapter, ...]:
    """Extract chapters defined by TOC entries.

    Args:
        doc: PyMuPDF document.
        toc: Simple TOC list [[level, title, page], ...].

    Returns:
        Tuple of Chapter objects.
    """
    # Filter to level-1 entries only
    level_1_entries = [entry for entry in toc if entry[0] == 1]

    if not level_1_entries:
        # No level-1 entries, fall back to heuristic
        return _extract_chapters_from_heuristic(doc)

    chapters = []
    for i, entry in enumerate(level_1_entries):
        title = entry[1].strip()
        start_page = max(0, entry[2] - 1)  # 0-based page index, clamped >= 0

        # End page is the start of the next chapter, or end of document
        if i + 1 < len(level_1_entries):
            end_page = max(0, level_1_entries[i + 1][2] - 1)
        else:
            end_page = doc.page_count

        paragraphs = _extract_paragraphs_from_range(doc, start_page, end_page, title)
        chapters.append(Chapter(title, paragraphs, True))

    return tuple(chapters)


def _extract_chapters_from_heuristic(doc: pymupdf.Document) -> tuple[Chapter, ...]:
    """Extract chapters by detecting large-text headings as chapter boundaries.

    Args:
        doc: PyMuPDF document.

    Returns:
        Tuple of Chapter objects, or single "Full text" chapter if no headings found.
    """
    body_size = _compute_body_text_size(doc)
    heading_threshold = body_size * 1.3
    chapter_starts = _find_heading_chapters(doc, heading_threshold)

    if not chapter_starts:
        return _fallback_full_text_chapter(doc)

    # Build chapters from detected headings
    chapters = []
    for i, (start_page, heading_title) in enumerate(chapter_starts):
        end_page = chapter_starts[i + 1][0] if i + 1 < len(chapter_starts) else doc.page_count
        paragraphs = _extract_paragraphs_from_range(
            doc, start_page, end_page, heading_title, exclude_heading=True
        )
        chapters.append(Chapter(heading_title, paragraphs, True))

    return tuple(chapters)


def _compute_body_text_size(doc: pymupdf.Document) -> float:
    """Compute median text size across the PDF document."""
    all_sizes: list[float] = []
    for page_num in range(doc.page_count):
        page = doc[page_num]
        text_dict = page.get_text("dict")  # type: ignore[no-untyped-call]
        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:  # text block
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        size = span.get("size", 0)
                        if size > 0:
                            all_sizes.append(size)

    return statistics.median(all_sizes) if all_sizes else 11.0


def _find_heading_chapters(
    doc: pymupdf.Document, heading_threshold: float
) -> list[tuple[int, str]]:
    """Find chapter boundaries by detecting large-text headings."""
    chapter_starts: list[tuple[int, str]] = []

    for page_num in range(doc.page_count):
        page = doc[page_num]
        text_dict = page.get_text("dict")  # type: ignore[no-untyped-call]

        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:  # text block
                for line in block.get("lines", []):
                    line_text = ""
                    max_size = 0.0

                    for span in line.get("spans", []):
                        line_text += span.get("text", "")
                        max_size = max(max_size, span.get("size", 0))

                    line_stripped = line_text.strip()
                    # Heading: large size, short text (< 80 chars), non-empty
                    if max_size >= heading_threshold and len(line_stripped) < 80 and line_stripped:
                        chapter_starts.append((page_num, line_stripped))

    return chapter_starts


def _extract_first_line(doc: pymupdf.Document) -> str | None:
    """Extract the first non-empty text line from the first page of a PDF.

    Args:
        doc: PyMuPDF document.

    Returns:
        The first non-empty line, or None if no text found.
    """
    if doc.page_count == 0:
        return None

    page = doc[0]
    blocks = page.get_text("blocks")  # type: ignore[no-untyped-call]

    # Sort blocks by (y, x) for reading order
    sorted_blocks = sorted(blocks, key=lambda b: (b[1], b[0]))

    for block in sorted_blocks:
        block_text = block[4].strip() if block[4] else ""

        # Skip empty blocks and page numbers
        if block_text and not block_text.isdigit():
            return block_text

    return None


def _fallback_full_text_chapter(doc: pymupdf.Document) -> tuple[Chapter, ...]:
    """Return a single untitled chapter if document has text, else empty."""
    for page_num in range(doc.page_count):
        paragraphs = _extract_paragraphs_from_range(doc, page_num, page_num + 1, "")
        if paragraphs:
            all_paragraphs = _extract_paragraphs_from_range(doc, 0, doc.page_count, "")
            return (Chapter("", all_paragraphs, False),)

    return ()


def _extract_paragraphs_from_range(
    doc: pymupdf.Document,
    start_page: int,
    end_page: int,
    heading_to_exclude: str = "",
    exclude_heading: bool = False,
) -> tuple[str, ...]:
    """Extract paragraph text from a range of pages.

    Args:
        doc: PyMuPDF document.
        start_page: Start page index (0-based, inclusive).
        end_page: End page index (0-based, exclusive).
        heading_to_exclude: The heading title to exclude from paragraphs (if exclude_heading=True).
        exclude_heading: Whether to exclude the heading line.

    Returns:
        Tuple of paragraph strings.
    """
    paragraphs: list[str] = []

    for page_num in range(start_page, end_page):
        if page_num < 0 or page_num >= doc.page_count:
            continue

        page = doc[page_num]
        blocks = page.get_text("blocks")  # type: ignore[no-untyped-call]

        # Sort blocks by (y, x) for reading order
        sorted_blocks = sorted(blocks, key=lambda b: (b[1], b[0]))

        for block in sorted_blocks:
            block_text = block[4].strip() if block[4] else ""

            # Skip empty blocks
            if not block_text:
                continue

            # Skip pure digits (page numbers)
            if block_text.isdigit():
                continue

            # Skip the heading line if requested
            if exclude_heading and block_text == heading_to_exclude:
                continue

            paragraphs.append(block_text)

    return tuple(paragraphs)


def convert_pdf_to_epub(
    pdf_path: Path, epub_path: Path, *, title: str | None = None, author: str | None = None
) -> ConversionResult:
    """Convert a PDF to EPUB with metadata, TOC, and cover image.

    Args:
        pdf_path: Path to the PDF file.
        epub_path: Path to write the EPUB file.
        title: Override the PDF's title (defaults to PDF title or filename stem).
        author: Override the PDF's author.

    Returns:
        ConversionResult with the converted title, author, chapter count, first line, and
        whether the title was a filename fallback (``title_from_filename``, EXP-224).

    Raises:
        PdfConversionError: If the PDF cannot be converted or has no text content.
    """
    book = extract_pdf_book(pdf_path)

    if not book.chapters:
        raise PdfConversionError("no text content found in PDF")

    # Treat default/placeholder titles from PDF extractors as "no title"
    meaningful_book_title = book.title if book.title and book.title.lower() != "untitled" else None

    # Effective title and author
    effective_title = title or meaningful_book_title or pdf_path.stem
    effective_author = author or book.author
    title_from_filename = not (title or meaningful_book_title)
    chapters = apply_title_to_untitled_chapters(book.chapters, effective_title)

    # Extract cover image and first line independently: a page that will not rasterise must
    # not take the document's own title candidate down with it (EXP-224).
    cover_bytes = None
    first_line = None
    doc = None
    try:
        doc = pymupdf.open(pdf_path)  # type: ignore[no-untyped-call]
        try:
            pix = doc[0].get_pixmap(dpi=110)
            cover_bytes = pix.tobytes("png")  # type: ignore[no-untyped-call]
        except Exception as exc:
            logger.warning("Cover render failed for %s: %s", pdf_path.name, exc)
        try:
            first_line = _extract_first_line(doc)
        except Exception as exc:
            logger.debug("First line extraction failed for %s: %s", pdf_path.name, exc)
    except Exception as exc:
        logger.warning(
            "PDF could not be reopened for cover and first line %s: %s", pdf_path.name, exc
        )
    finally:
        if doc is not None:
            doc.close()  # type: ignore[no-untyped-call]

    if title_from_filename:
        logger.debug(
            "PDF title fell back to the filename: %r (document first line: %r)",
            effective_title,
            first_line,
        )

    result = build_epub(
        Book(title=effective_title, author=effective_author, chapters=chapters),
        epub_path,
        cover_bytes=cover_bytes,
        first_line=first_line,
        title_from_filename=title_from_filename,
    )

    logger.info(
        'Converted PDF to EPUB: "%s" (%d chapters, cover=%s)',
        effective_title,
        len(book.chapters),
        bool(cover_bytes),
    )

    return result
