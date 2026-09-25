"""Tests for EPUB merge input reader (neutral model population from disk).

Tests validate that read_input_book correctly opens EPUBs from disk and populates
the InputBook neutral model for the merge's planning functions.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from epub_merge.merge.errors import MergeInputError
from epub_merge.merge.reader import read_input_book


class TestReadInputBook:
    """Tests for read_input_book function."""

    def test_reads_fanficfare_shaped_book(self, build_epub: object, tmp_path: Path) -> None:
        """Read a FanFicFare-shaped EPUB (NCX-backed) into InputBook model."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1"), ("Ch. 2", "u2")], filename="a.epub", doc_title="Book A")

        book = read_input_book(path, 0)

        assert book.index == 0
        assert book.name == "a.epub"
        assert book.version == "2.0"
        assert book.title == "Book A"
        assert book.language == "en"
        assert len(book.chapters) == 2
        assert [c.label for c in book.chapters] == ["Ch. 1", "Ch. 2"]
        assert book.content_root == "OEBPS"

    def test_chapter_bytes_are_loaded(self, build_epub: object, tmp_path: Path) -> None:
        """Chapter xhtml bytes are correctly loaded from the archive."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1"), ("Ch. 2", "u2")], doc_title="Book A")

        book = read_input_book(path, 0)

        assert b"Body." in book.chapters[0].xhtml

    def test_title_page_is_not_a_chapter(self, build_epub: object, tmp_path: Path) -> None:
        """Title page (item_id='title_page') is excluded from chapters."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1")], doc_title="Book A")

        book = read_input_book(path, 0)

        assert not any(c.item_id == "title_page" for c in book.chapters)

    def test_stylesheet_is_a_resource_not_a_chapter(
        self, build_epub: object, tmp_path: Path
    ) -> None:
        """Stylesheet appears in resources, not chapters."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1")], doc_title="Book A")

        book = read_input_book(path, 0)

        assert book.stylesheets == ("OEBPS/stylesheet.css",)
        assert any(
            r.href == "OEBPS/stylesheet.css" and r.media_type == "text/css" for r in book.resources
        )

    def test_ncx_is_not_a_resource(self, build_epub: object, tmp_path: Path) -> None:
        """NCX document is not included in resources."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1")], doc_title="Book A")

        book = read_input_book(path, 0)

        assert not any(r.media_type == "application/x-dtbncx+xml" for r in book.resources)

    def test_reads_nav_only_epub3_book(self, build_nav_epub: object, tmp_path: Path) -> None:
        """Read an EPUB3 nav-only book (no toc.ncx) into InputBook model."""
        factory = build_nav_epub  # type: ignore[assignment]
        path = factory([("Chapter 1", "u1")], doc_title="AIF 36")

        book = read_input_book(path, 0)

        assert book.version == "3.0"
        assert len(book.chapters) == 1
        assert book.chapters[0].label == "Chapter 1"
        assert book.content_root == ""
        assert not any("nav" in r.href for r in book.resources)

    def test_reads_real_fixture_metadata(self) -> None:
        """Read real fixture (tending_bar.epub) and verify metadata extraction."""
        path = Path(__file__).resolve().parent / "fixtures" / "tending_bar.epub"

        book = read_input_book(path, 0)

        assert book.creators == (("Moosetales", None),)
        assert book.source == "https://www.literotica.com/series/se/495173223"
        assert book.publisher == "literotica.com"
        assert len(book.subjects) == 5
        assert len(book.chapters) == 6
        assert book.identifier == "fanficfare-uid:literotica.com-ustories-s495173223"

    def test_cover_fields_none_without_cover(self, build_epub: object, tmp_path: Path) -> None:
        """Cover fields are None when EPUB has no cover image."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1")], doc_title="Book A")

        book = read_input_book(path, 0)

        assert book.cover_href is None
        assert book.cover_media_type is None

    def test_cover_fields_set_when_cover_present(self, build_epub: object, tmp_path: Path) -> None:
        """Cover href and media_type are populated when cover image exists."""
        factory = build_epub  # type: ignore[assignment]
        # Build a cover image (minimal PNG)
        cover_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        path = factory([("Ch. 1", "u1")], doc_title="Book A", cover=cover_data)

        book = read_input_book(path, 0)

        assert book.cover_href is not None
        assert book.cover_href.endswith("cover.jpg")
        assert book.cover_media_type == "image/jpeg"

    def test_dangling_spine_reference_raises(self, build_epub: object, tmp_path: Path) -> None:
        """Spine reference to non-existent manifest item raises MergeInputError."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1")], doc_title="Book A")

        # Modify the OPF to add a dangling spine reference
        from ebookerr_sdk.epub import EpubDocument

        doc = EpubDocument.open(path)
        doc.opf.add_spine_item("ghost")
        doc.save()

        with pytest.raises(MergeInputError, match="ghost"):
            read_input_book(path, 0)

    def test_chapters_match_content_chapters(self, build_epub: object, tmp_path: Path) -> None:
        """Chapters read by reader match EpubDocument.content_chapters() order and IDs."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1"), ("Ch. 2", "u2")], doc_title="Book A")

        book = read_input_book(path, 0)
        from ebookerr_sdk.epub import EpubDocument

        doc = EpubDocument.open(path)
        assert (
            [c.item_id for c in book.chapters]
            == [i.id for i in doc.content_chapters()]
            == [
                "file0001",
                "file0002",
            ]
        )

    def test_dangling_non_linear_spine_reference_is_tolerated(
        self, build_epub: object, tmp_path: Path
    ) -> None:
        """Non-linear spine reference (linear=no) is excluded from dangling check."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1")], doc_title="Book A")

        from ebookerr_sdk.epub import EpubDocument

        doc = EpubDocument.open(path)
        doc.opf.add_spine_item("ghost", linear="no")
        doc.save()

        book = read_input_book(path, 0)
        assert [c.item_id for c in book.chapters] == ["file0001"]

    def test_missing_chapter_file_raises(self, build_epub: object, tmp_path: Path) -> None:
        """Missing chapter file raises MergeInputError."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1"), ("Ch. 2", "u2")], doc_title="Book A")

        # Corrupt the EPUB by removing a chapter file from the zip
        import zipfile

        temp_path = tmp_path / "temp.epub"
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(temp_path, "w") as zout:
            for item in zin.infolist():
                if item.filename != "OEBPS/file0002.xhtml":  # Remove second chapter
                    zout.writestr(item, zin.read(item.filename))
        temp_path.replace(path)

        with pytest.raises(MergeInputError, match="missing file"):
            read_input_book(path, 0)

    def test_missing_resource_is_skipped_with_warning(
        self, build_epub: object, tmp_path: Path, caplog: object
    ) -> None:
        """Missing resource file logs warning; read succeeds with resource omitted."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1")], doc_title="Book A")

        # Corrupt the EPUB by removing the stylesheet
        import zipfile

        temp_path = tmp_path / "temp.epub"
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(temp_path, "w") as zout:
            for item in zin.infolist():
                if item.filename != "OEBPS/stylesheet.css":
                    zout.writestr(item, zin.read(item.filename))
        temp_path.replace(path)

        caplog_obj = caplog  # type: ignore[assignment]
        with caplog_obj.at_level(logging.WARNING):
            book = read_input_book(path, 0)

        assert not any(r.href == "OEBPS/stylesheet.css" for r in book.resources)
        assert any("skipping missing resource" in record.message for record in caplog_obj.records)

    def test_unreadable_epub_raises_merge_input_error(self, tmp_path: Path) -> None:
        """Non-EPUB file raises MergeInputError."""
        bad_path = tmp_path / "not_epub.epub"
        bad_path.write_bytes(b"not a zip")

        with pytest.raises(MergeInputError):
            read_input_book(bad_path, 0)

    def test_logs_debug_summary(self, build_epub: object, tmp_path: Path, caplog: object) -> None:
        """Reading a book emits debug log with index, name, version, chapter/resource counts."""
        factory = build_epub  # type: ignore[assignment]
        path = factory([("Ch. 1", "u1"), ("Ch. 2", "u2")], filename="test.epub", doc_title="Book")

        caplog_obj = caplog  # type: ignore[assignment]
        with caplog_obj.at_level(logging.DEBUG):
            read_input_book(path, 0)

        log_text = " ".join(record.message for record in caplog_obj.records)
        assert "Merge input 0" in log_text
        assert "2 chapter(s)" in log_text

    def test_content_root_empty_for_flat_book(self, build_nav_epub: object, tmp_path: Path) -> None:
        """Content root is empty string when chapters are at OPF directory level."""
        factory = build_nav_epub  # type: ignore[assignment]
        path = factory([("Chapter 1", "u1")], doc_title="Flat Book")

        book = read_input_book(path, 0)

        assert book.content_root == ""
