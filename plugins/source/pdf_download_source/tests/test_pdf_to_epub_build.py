"""Tests for PDF→EPUB assembly with metadata, NCX TOC, and cover."""

from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

import pymupdf
import pytest
from ebookerr_sdk.epub.build import DocumentConversionError
from ebookerr_sdk.epub.document import EpubDocument
from pdf_download_source.pdf_to_epub import (
    PdfConversionError,
    convert_pdf_to_epub,
)


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


def test_convert_roundtrip_metadata(tmp_path: Path) -> None:
    """Synthetic PDF (title "My Book", author "Jane", 2 chapters) → EPUB → metadata round-trips."""
    pdf_path = _make_pdf(
        tmp_path,
        [
            "Page 1 content for chapter 1",
            "Page 2 content still in chapter 1",
            "Page 3 content for chapter 2",
        ],
        toc=[[1, "Ch 1", 1], [1, "Ch 2", 3]],
        meta={"title": "My Book", "author": "Jane"},
    )
    epub_path = tmp_path / "output.epub"

    result = convert_pdf_to_epub(pdf_path, epub_path)

    # Check result object
    assert result.title == "My Book"
    assert result.author == "Jane"
    assert result.source_chapter_count == 2

    # Validate with EpubDocument reader
    doc = EpubDocument.open(epub_path)
    metadata = doc.read_metadata()
    assert metadata.title == "My Book"
    assert metadata.author == "Jane"
    assert metadata.chapter_count == 2


def test_convert_param_overrides_win(tmp_path: Path) -> None:
    """Params override PDF metadata: title and author are passed explicitly."""
    pdf_path = _make_pdf(
        tmp_path,
        ["Content"],
        meta={"title": "PDF Title", "author": "PDF Author"},
    )
    epub_path = tmp_path / "output.epub"

    result = convert_pdf_to_epub(pdf_path, epub_path, title="Override", author="A2")

    assert result.title == "Override"
    assert result.author == "A2"

    doc = EpubDocument.open(epub_path)
    metadata = doc.read_metadata()
    assert metadata.title == "Override"
    assert metadata.author == "A2"


def test_convert_mimetype_first_and_stored(tmp_path: Path) -> None:
    """The mimetype entry is first in the zip and compressed with ZIP_STORED."""
    pdf_path = _make_pdf(tmp_path, ["Content"])
    epub_path = tmp_path / "output.epub"

    convert_pdf_to_epub(pdf_path, epub_path)

    with ZipFile(epub_path) as zf:
        info_list = zf.infolist()
        assert len(info_list) > 0
        assert info_list[0].filename == "mimetype"
        assert info_list[0].compress_type == ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"


def test_convert_chapter_xhtml_escaped(tmp_path: Path) -> None:
    """Paragraph text containing XML special chars is escaped in XHTML."""
    pdf_path = _make_pdf(
        tmp_path,
        ["Text with <b>&\"'"],
        toc=[[1, "Ch 1", 1]],
    )
    epub_path = tmp_path / "output.epub"

    convert_pdf_to_epub(pdf_path, epub_path)

    with ZipFile(epub_path) as zf:
        xhtml_content = zf.read("OEBPS/chapter_001.xhtml").decode("utf-8")

    # Should be escaped
    assert "&lt;b&gt;" in xhtml_content
    assert "&amp;" in xhtml_content
    assert "&quot;" in xhtml_content or "&#34;" in xhtml_content
    assert "&apos;" in xhtml_content or "&#39;" in xhtml_content
    # The literal <b> should NOT appear unescaped
    assert "<b>" not in xhtml_content or "&lt;b&gt;" in xhtml_content


def test_convert_cover_present(tmp_path: Path) -> None:
    """Cover image from PDF page 1 is written to OEBPS/cover.png and OPF has meta cover."""
    pdf_path = _make_pdf(tmp_path, ["Page with content"])
    epub_path = tmp_path / "output.epub"

    convert_pdf_to_epub(pdf_path, epub_path)

    with ZipFile(epub_path) as zf:
        # Cover file should exist
        assert "OEBPS/cover.png" in zf.namelist()
        cover_data = zf.read("OEBPS/cover.png")
        assert len(cover_data) > 0  # Non-empty PNG

        # OPF should have meta name="cover"
        opf_content = zf.read("OEBPS/content.opf").decode("utf-8")
        assert 'meta name="cover"' in opf_content


def test_convert_ncx_navpoints(tmp_path: Path) -> None:
    """toc.ncx has navPoints matching chapters with correct labels."""
    pdf_path = _make_pdf(
        tmp_path,
        [
            "Ch 1 content",
            "Ch 2 content",
        ],
        toc=[[1, "Ch 1", 1], [1, "Ch 2", 2]],
    )
    epub_path = tmp_path / "output.epub"

    convert_pdf_to_epub(pdf_path, epub_path)

    with ZipFile(epub_path) as zf:
        ncx_content = zf.read("OEBPS/toc.ncx").decode("utf-8")

    # Parse and verify navPoints
    root = ET.fromstring(ncx_content)
    ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
    nav_points = root.findall(".//ncx:navPoint", ns)

    # Should have 3 navPoints: 1 title page + 2 chapters
    assert len(nav_points) == 3

    # Extract labels (skip title page)
    labels = []
    for point in nav_points[1:]:  # Skip the title page (index 0)
        label_elem = point.find(".//ncx:navLabel/ncx:text", ns)
        if label_elem is not None:
            labels.append(label_elem.text)

    assert labels == ["Ch 1", "Ch 2"]


def test_convert_pdf_no_headings_no_full_text_parasite(tmp_path: Path) -> None:
    """A PDF with no TOC/headings produces no 'Full text' title/heading anywhere."""
    pdf_path = _make_pdf(
        tmp_path,
        ["Body text line 1.", "Body text line 2."],
        meta={"title": "My Book"},
    )
    epub_path = tmp_path / "output.epub"

    result = convert_pdf_to_epub(pdf_path, epub_path)

    assert result.title == "My Book"

    with ZipFile(epub_path) as zf:
        xhtml_text = zf.read("OEBPS/chapter_001.xhtml").decode("utf-8")
        ncx_text = zf.read("OEBPS/toc.ncx").decode("utf-8")

    assert "Full text" not in xhtml_text
    assert "Full text" not in ncx_text
    assert "<h1>" not in xhtml_text
    assert "<title>My Book</title>" in xhtml_text
    assert "<text>My Book</text>" in ncx_text


def test_convert_empty_pdf_raises(tmp_path: Path) -> None:
    """Blank-page PDF (no text) raises PdfConversionError."""
    pdf_path = _make_pdf(tmp_path, ["", ""])  # Two blank pages
    epub_path = tmp_path / "output.epub"

    with pytest.raises(PdfConversionError, match="no text content found in PDF"):
        convert_pdf_to_epub(pdf_path, epub_path)


def test_pdf_conversion_error_is_document_conversion_error() -> None:
    """PdfConversionError is a subclass of DocumentConversionError."""
    assert issubclass(PdfConversionError, DocumentConversionError)
