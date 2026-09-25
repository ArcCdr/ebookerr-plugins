"""Tests for PyMuPDF-based PDF text/chapter extraction."""

from pathlib import Path

import pymupdf
import pytest
from pdf_download_source.pdf_to_epub import PdfConversionError, extract_pdf_book


def _make_pdf(
    tmp_path: Path,
    pages: list[str],
    toc: list[list] | None = None,
    meta: dict | None = None,
) -> Path:
    """Helper to build synthetic PDFs for testing.

    Args:
        tmp_path: Directory to create the PDF in.
        pages: List of text strings to insert on each page.
        toc: Optional table of contents [[level, title, page], ...].
        meta: Optional metadata dict with 'title' and/or 'author' keys.

    Returns:
        Path to the created PDF.
    """
    pdf_path = tmp_path / "test.pdf"
    doc = pymupdf.open()

    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=11)

    if toc:
        doc.set_toc(toc)

    if meta:
        doc.set_metadata(meta)

    doc.save(pdf_path)
    doc.close()

    return pdf_path


def test_extract_metadata(tmp_path: Path) -> None:
    """PDF with metadata title and author extracts correctly."""
    pdf_path = _make_pdf(
        tmp_path,
        ["Chapter 1 text"],
        meta={"title": "My Book", "author": "Jane"},
    )

    book = extract_pdf_book(pdf_path)

    assert book.title == "My Book"
    assert book.author == "Jane"


def test_extract_toc_chapters(tmp_path: Path) -> None:
    """TOC defines chapter boundaries; text per chapter extracted correctly."""
    # 4 pages with TOC: Ch 1 at page 1, Ch 2 at page 3
    pdf_path = _make_pdf(
        tmp_path,
        [
            "Page 1 content",
            "Page 2 content",
            "Page 3 content",
            "Page 4 content",
        ],
        toc=[[1, "Ch 1", 1], [1, "Ch 2", 3]],
    )

    book = extract_pdf_book(pdf_path)

    assert len(book.chapters) == 2
    assert book.chapters[0].title == "Ch 1"
    assert book.chapters[0].has_heading is True
    assert book.chapters[1].title == "Ch 2"
    assert book.chapters[1].has_heading is True
    # Ch 1 should have text from pages 1-2
    assert "Page 1 content" in book.chapters[0].paragraphs
    assert "Page 2 content" in book.chapters[0].paragraphs
    assert "Page 3 content" not in book.chapters[0].paragraphs
    # Ch 2 should have text from pages 3-4
    assert "Page 3 content" in book.chapters[1].paragraphs
    assert "Page 4 content" in book.chapters[1].paragraphs


def test_extract_heuristic_headings(tmp_path: Path) -> None:
    """Without TOC, large-text lines become chapter titles via heuristic."""
    # Create a PDF with a heading (fontsize 22) and body (fontsize 11)
    pdf_path = tmp_path / "test.pdf"
    doc = pymupdf.open()

    page = doc.new_page()
    page.insert_text((72, 72), "CHAPTER ONE", fontsize=22)
    page.insert_text((72, 120), "This is body text.", fontsize=11)
    page.insert_text((72, 150), "More body text.", fontsize=11)

    doc.save(pdf_path)
    doc.close()

    book = extract_pdf_book(pdf_path)

    # Should detect "CHAPTER ONE" as a chapter title via fontsize heuristic
    assert len(book.chapters) >= 1
    assert book.chapters[0].title == "CHAPTER ONE"
    assert book.chapters[0].has_heading is True
    # Chapter body should contain the body text, not the heading
    assert "This is body text." in book.chapters[0].paragraphs
    assert "More body text." in book.chapters[0].paragraphs


def test_extract_single_chapter_fallback(tmp_path: Path) -> None:
    """Uniform body text with no TOC or large headings → one untitled chapter."""
    pdf_path = _make_pdf(
        tmp_path,
        [
            "Body text line 1.",
            "Body text line 2.",
        ],
    )

    book = extract_pdf_book(pdf_path)

    # No TOC, no large headings → single untitled, has_heading=False chapter
    assert len(book.chapters) == 1
    assert book.chapters[0].title == ""
    assert book.chapters[0].has_heading is False
    assert "Body text line 1." in book.chapters[0].paragraphs
    assert "Body text line 2." in book.chapters[0].paragraphs


def test_extract_skips_page_numbers(tmp_path: Path) -> None:
    """Blocks that are pure digits (page numbers) are skipped."""
    pdf_path = tmp_path / "test.pdf"
    doc = pymupdf.open()

    page = doc.new_page()
    page.insert_text((72, 72), "Body content here.", fontsize=11)
    page.insert_text((500, 700), "17", fontsize=11)  # Page number

    doc.save(pdf_path)
    doc.close()

    book = extract_pdf_book(pdf_path)

    # "17" should be skipped from paragraphs
    paragraphs = book.chapters[0].paragraphs
    assert "Body content here." in paragraphs
    assert "17" not in paragraphs


def test_extract_invalid_pdf_raises(tmp_path: Path) -> None:
    """Non-PDF file → PdfConversionError."""
    bad_pdf = tmp_path / "not_a_pdf.pdf"
    bad_pdf.write_text("This is not a PDF file.")

    with pytest.raises(PdfConversionError, match="cannot open PDF"):
        extract_pdf_book(bad_pdf)
