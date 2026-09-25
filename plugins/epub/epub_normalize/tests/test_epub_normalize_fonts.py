"""Tests for unreferenced font removal during EPUB normalization."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from ebookerr_sdk.epub.document import EpubDocument
from epub_normalize.normalize.counts import NormalizeCounts
from epub_normalize.normalize.fonts import sweep_fonts


def _add_font(
    doc_path: Path,
    item_id: str,
    href: str,
    media_type: str,
    data: bytes,
) -> None:
    """Helper: open EPUB, add a font manifest item, write bytes, and save."""
    doc = EpubDocument.open(doc_path)
    doc.opf.add_manifest_item(item_id, href, media_type)
    doc.write_resource(href, data)
    doc.save()


class TestFontRemoval:
    """Tests for sweep_fonts basic removal logic."""

    def test_unreferenced_font_is_removed(self, build_epub: Callable[..., Path]) -> None:
        """A font with no references anywhere is removed from manifest and archive."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is None
        assert "OEBPS/foo.ttf" not in doc.member_names()
        assert counts.font_files_removed == 1

    def test_referenced_font_is_kept(self, build_epub: Callable[..., Path]) -> None:
        """A font referenced in CSS is kept."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        doc.write_member("OEBPS/stylesheet.css", b"@font-face { src: url(foo.ttf); }")
        doc.save()

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is not None
        assert "OEBPS/foo.ttf" in doc.member_names()
        assert counts.font_files_removed == 0

    def test_font_referenced_from_a_chapter_is_kept(self, build_epub: Callable[..., Path]) -> None:
        """A font referenced in chapter XHTML is kept."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        # Add reference to font in chapter content
        chapter = doc.chapter("OEBPS/file0001.xhtml")
        xhtml = chapter.to_xml()
        # Insert font reference into body
        xhtml = xhtml.replace("<p>Body.</p>", "<p>Body. See foo.ttf</p>")
        doc.write_chapter("OEBPS/file0001.xhtml", chapter.__class__.parse(xhtml))
        doc.save()

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is not None
        assert "OEBPS/foo.ttf" in doc.member_names()
        assert counts.font_files_removed == 0

    def test_font_referenced_from_an_svg_is_kept(self, build_epub: Callable[..., Path]) -> None:
        """A font referenced in SVG is kept."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        doc.opf.add_manifest_item("title_svg", "OEBPS/title.svg", "image/svg+xml")
        doc.write_member("OEBPS/title.svg", b"<svg>See foo.ttf</svg>")
        doc.save()

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is not None
        assert "OEBPS/foo.ttf" in doc.member_names()
        assert counts.font_files_removed == 0

    def test_reference_match_is_case_insensitive(self, build_epub: Callable[..., Path]) -> None:
        """Reference matching is case-insensitive."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        doc.write_member("OEBPS/stylesheet.css", b"@font-face { src: url(FOO.TTF); }")
        doc.save()

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is not None
        assert "OEBPS/foo.ttf" in doc.member_names()
        assert counts.font_files_removed == 0

    def test_detection_by_extension_when_media_type_lies(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Font detection works by extension even if media type is wrong."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/bar.woff2", "application/octet-stream", b"FONTDATA")

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is None
        assert "OEBPS/bar.woff2" not in doc.member_names()
        assert counts.font_files_removed == 1

    def test_detection_by_media_type_when_extension_is_odd(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Font detection works by media type even if extension is odd."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/bar.bin", "application/vnd.ms-opentype", b"FONTDATA")

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is None
        assert "OEBPS/bar.bin" not in doc.member_names()
        assert counts.font_files_removed == 1

    def test_no_fonts_is_a_noop(self, build_epub: Callable[..., Path]) -> None:
        """Sweep completes without error when there are no fonts."""
        path = build_epub([("Ch. 1", "url1")])

        doc = EpubDocument.open(path)
        members_before = doc.member_names()
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.member_names() == members_before
        assert counts.font_files_removed == 0

    def test_two_fonts_one_referenced(self, build_epub: Callable[..., Path]) -> None:
        """With two fonts, only the unreferenced one is removed."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA1")
        _add_font(path, "f2", "OEBPS/keep.ttf", "font/ttf", b"FONTDATA2")

        doc = EpubDocument.open(path)
        doc.write_member("OEBPS/stylesheet.css", b"@font-face { src: url(keep.ttf); }")
        doc.save()

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.opf.item_by_id("f1") is None
        assert doc.opf.item_by_id("f2") is not None
        assert "OEBPS/foo.ttf" not in doc.member_names()
        assert "OEBPS/keep.ttf" in doc.member_names()
        assert counts.font_files_removed == 1


class TestEncryptionXmlHandling:
    """Tests for META-INF/encryption.xml pruning."""

    def test_encryption_xml_removed_when_all_entries_go(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """When all encrypted fonts are removed, encryption.xml is deleted."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        encryption_xml = (
            b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b"<EncryptedData>"
            b'<CipherReference URI="OEBPS/foo.ttf"/>'
            b"</EncryptedData>"
            b"</encryption>"
        )
        doc.write_member("META-INF/encryption.xml", encryption_xml)
        doc.save()

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert doc.member_bytes("META-INF/encryption.xml") is None

    def test_encryption_xml_pruned_when_an_entry_survives(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """When some encrypted fonts survive, their entries stay in encryption.xml."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA1")
        _add_font(path, "f2", "OEBPS/keep.ttf", "font/ttf", b"FONTDATA2")

        doc = EpubDocument.open(path)
        doc.write_member("OEBPS/stylesheet.css", b"@font-face { src: url(keep.ttf); }")
        encryption_xml = (
            b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b"<EncryptedData>"
            b'<CipherReference URI="OEBPS/foo.ttf"/>'
            b"</EncryptedData>"
            b"<EncryptedData>"
            b'<CipherReference URI="OEBPS/keep.ttf"/>'
            b"</EncryptedData>"
            b"</encryption>"
        )
        doc.write_member("META-INF/encryption.xml", encryption_xml)
        doc.save()

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        encryption_bytes = doc.member_bytes("META-INF/encryption.xml")
        assert encryption_bytes is not None
        encryption_text = encryption_bytes.decode("utf-8", errors="replace")
        assert "foo.ttf" not in encryption_text
        assert "keep.ttf" in encryption_text

    def test_encryption_xml_untouched_when_no_font_is_removed(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """When no fonts are removed, encryption.xml is left byte-identical."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/keep.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        doc.write_member("OEBPS/stylesheet.css", b"@font-face { src: url(keep.ttf); }")
        encryption_xml = (
            b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b"<EncryptedData>"
            b'<CipherReference URI="OEBPS/keep.ttf"/>'
            b"</EncryptedData>"
            b"</encryption>"
        )
        doc.write_member("META-INF/encryption.xml", encryption_xml)
        doc.save()

        doc = EpubDocument.open(path)
        encryption_before = doc.member_bytes("META-INF/encryption.xml")
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        encryption_after = doc.member_bytes("META-INF/encryption.xml")
        assert encryption_before == encryption_after

    def test_no_encryption_xml_is_fine(self, build_epub: Callable[..., Path]) -> None:
        """Sweep completes without error when there's no encryption.xml."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)

        assert counts.font_files_removed == 1


class TestLogging:
    """Tests for debug logging."""

    def test_debug_line_names_the_removed_font(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Removing a font logs a debug message with href and item_id."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        with caplog.at_level(logging.DEBUG):
            counts = NormalizeCounts()
            sweep_fonts(doc, counts=counts)

        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Normalize removed unreferenced font" in r.message for r in debug_records)
        assert any("OEBPS/foo.ttf" in r.message for r in debug_records)


class TestRoundtrip:
    """Tests for save/reload behavior."""

    def test_removal_survives_a_save_roundtrip(self, build_epub: Callable[..., Path]) -> None:
        """Removed fonts stay removed after save and re-open."""
        path = build_epub([("Ch. 1", "url1")])
        _add_font(path, "f1", "OEBPS/foo.ttf", "font/ttf", b"FONTDATA")

        doc = EpubDocument.open(path)
        counts = NormalizeCounts()
        sweep_fonts(doc, counts=counts)
        doc.save()

        doc = EpubDocument.open(path)
        assert doc.opf.item_by_id("f1") is None
        assert "OEBPS/foo.ttf" not in doc.member_names()
