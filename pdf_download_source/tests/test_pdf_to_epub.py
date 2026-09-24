"""Tests for PDF→EPUB converter."""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf
import pytest
from ebookerr_sdk.epub.build import DocumentConversionError
from pdf_download_source.pdf_to_epub import (
    PdfConversionError,
    _extract_first_line,
    convert_pdf_to_epub,
)


def _make_minimal_pdf(
    tmp_path: Path, filename: str = "in.pdf", text: str = "Sample PDF text"
) -> Path:
    """Create a minimal PDF with the given text using reportlab.

    Args:
        tmp_path: Temporary directory path.
        filename: Name of the PDF file.
        text: Text to write in the PDF.

    Returns:
        Path to the created PDF file.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")

    pdf_path = tmp_path / filename
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    c.drawString(72, 750, text)
    c.save()

    return pdf_path


def test_pdf_conversion_error_is_document_conversion_error() -> None:
    """PdfConversionError is a DocumentConversionError subclass."""
    assert issubclass(PdfConversionError, DocumentConversionError)


def test_conversion_result_carries_the_first_line(tmp_path: Path) -> None:
    """ConversionResult.first_line is the document's first text line (EXP-224)."""
    pdf_path = _make_minimal_pdf(tmp_path, text="First line of PDF content")
    epub_path = tmp_path / "out.epub"

    result = convert_pdf_to_epub(pdf_path, epub_path)

    assert result.first_line == "First line of PDF content"


def test_extract_first_line_is_none_for_a_zero_page_document() -> None:
    """_extract_first_line returns None when the document has no pages."""
    doc = pymupdf.open()
    try:
        assert _extract_first_line(doc) is None
    finally:
        doc.close()


def test_extract_first_line_skips_a_leading_page_number_block() -> None:
    """_extract_first_line skips a digit-only block (a page number) for the real text below it."""
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "42")
        page.insert_text((72, 200), "Real first line")

        assert _extract_first_line(doc) == "Real first line"
    finally:
        doc.close()


def test_extract_first_line_is_none_when_every_block_is_a_page_number() -> None:
    """_extract_first_line returns None when the page has no non-page-number text."""
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "42")

        assert _extract_first_line(doc) is None
    finally:
        doc.close()


def test_a_pdf_with_no_embedded_title_reports_the_filename_fallback(tmp_path: Path) -> None:
    """A PDF with no title uses filename stem and sets title_from_filename=True (EXP-224)."""
    pdf_path = _make_minimal_pdf(tmp_path, filename="pg1342.pdf", text="Pride and Prejudice")
    epub_path = tmp_path / "out.epub"

    result = convert_pdf_to_epub(pdf_path, epub_path)

    assert result.title == "pg1342"
    assert result.title_from_filename is True
    assert result.first_line == "Pride and Prejudice"


def test_an_explicit_pdf_title_is_never_a_filename_fallback(tmp_path: Path) -> None:
    """When title is explicitly passed, title_from_filename=False (EXP-224)."""
    pdf_path = _make_minimal_pdf(tmp_path, filename="pg1342.pdf", text="Pride and Prejudice")
    epub_path = tmp_path / "out.epub"

    result = convert_pdf_to_epub(pdf_path, epub_path, title="Given Title")

    assert result.title == "Given Title"
    assert result.title_from_filename is False


def test_a_cover_render_failure_does_not_lose_the_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed cover render does not prevent first_line extraction (EXP-224)."""
    pdf_path = _make_minimal_pdf(tmp_path, filename="pg1342.pdf", text="Pride and Prejudice")
    epub_path = tmp_path / "out.epub"

    def raise_on_pixmap(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", raise_on_pixmap)

    with caplog.at_level(logging.WARNING):
        result = convert_pdf_to_epub(pdf_path, epub_path)

    assert result.first_line == "Pride and Prejudice"
    assert any("Cover render failed" in record.message for record in caplog.records)


def test_the_pdf_filename_fallback_is_logged_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A PDF using filename as title logs at DEBUG level (EXP-224)."""
    pdf_path = _make_minimal_pdf(tmp_path, filename="pg1342.pdf", text="Pride and Prejudice")
    epub_path = tmp_path / "out.epub"

    with caplog.at_level(logging.DEBUG):
        convert_pdf_to_epub(pdf_path, epub_path)

    assert any("PDF title fell back to the filename" in record.message for record in caplog.records)
