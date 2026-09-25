"""Tests for the EPUB normalize orchestrator and font-sweep wiring."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from ebookerr_sdk.epub.document import EpubDocument
from epub_normalize.normalize import (
    NormalizeOptions,
    normalize_epub,
)


def _add_font(path: Path, item_id: str, href: str, media_type: str = "font/ttf") -> None:
    """Register a font manifest item with dummy bytes and save.

    Args:
        path: Path to the EPUB file.
        item_id: The manifest item ID for the font.
        href: The manifest href for the font.
        media_type: The MIME type for the font (default: font/ttf).
    """
    doc = EpubDocument.open(path)
    doc.write_resource(href, b"FONTDATA")
    doc.opf.add_manifest_item(item_id, href, media_type)
    doc.save()


def _set_css(path: Path, css: str) -> None:
    """Overwrite the book's stylesheet and save.

    Args:
        path: Path to the EPUB file.
        css: The CSS content to write.
    """
    doc = EpubDocument.open(path)
    doc.write_resource("OEBPS/stylesheet.css", css.encode("utf-8"))
    doc.save()


class TestFontSweepWiring:
    """Tests for font-sweep wiring in the EPUB normalize orchestrator."""

    def test_font_dropped_after_its_font_face_rule_is_stripped(self, build_epub):
        """Font is dropped when its @font-face rule is stripped by CSS normalization.

        This test verifies the ordering: the sweep runs after CSS rewrite, so it sees
        the rewritten CSS without the @font-face rule and correctly identifies the
        font as unreferenced.
        """
        path = build_epub([("Chapter 1", "https://example.test/1")])

        # Add a font and reference it in the stylesheet
        _add_font(path, "f1", "OEBPS/foo.ttf")
        _set_css(
            path,
            '@font-face { font-family: "Foo"; src: url(foo.ttf); }\n'
            'body { font-family: "Foo"; margin: 2% }',
        )

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.counts.font_files_removed == 1
        doc = EpubDocument.open(path)
        assert doc.opf.item_by_id("f1") is None
        assert "OEBPS/foo.ttf" not in doc.member_names()

    def test_font_kept_when_font_stripping_is_disabled(self, build_epub):
        """Font survives when strip_fonts option is False.

        Both the @font-face rule and the font file survive.
        """
        path = build_epub([("Chapter 1", "https://example.test/1")])

        _add_font(path, "f1", "OEBPS/foo.ttf")
        _set_css(
            path,
            '@font-face { font-family: "Foo"; src: url(foo.ttf); }\n'
            'body { font-family: "Foo"; margin: 2% }',
        )

        report = normalize_epub(
            path, options=dataclasses.replace(NormalizeOptions(), strip_fonts=False)
        )

        assert report.counts.font_files_removed == 0
        doc = EpubDocument.open(path)
        assert doc.opf.item_by_id("f1") is not None
        assert "OEBPS/foo.ttf" in doc.member_names()

    def test_font_kept_when_a_chapter_still_references_it(self, build_epub):
        """Font survives when a chapter's XHTML text still references it.

        No @font-face rule in CSS, but the XHTML content contains a reference to the
        font filename.
        """
        path = build_epub([("Chapter 1", "https://example.test/1")])

        _add_font(path, "f1", "OEBPS/foo.ttf")
        _set_css(path, "body { margin: 2% }")

        # Re-open and add a reference to the font in the chapter text
        doc = EpubDocument.open(path)
        chapter_content = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")
        # Insert the font reference in the chapter
        modified_chapter = chapter_content.replace(
            "<p>Body.</p>",
            "<p>Body.</p>\n<!-- Reference: foo.ttf -->",
        )
        doc.write_resource("OEBPS/file0001.xhtml", modified_chapter.encode("utf-8"))
        doc.save()

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.counts.font_files_removed == 0
        doc = EpubDocument.open(path)
        assert doc.opf.item_by_id("f1") is not None
        assert "OEBPS/foo.ttf" in doc.member_names()

    def test_book_still_opens_after_a_font_removal(self, build_epub):
        """EPUB remains valid and openable after font removal."""
        path = build_epub([("Chapter 1", "https://example.test/1")])

        _add_font(path, "f1", "OEBPS/foo.ttf")
        _set_css(
            path,
            '@font-face { font-family: "Foo"; src: url(foo.ttf); }\n'
            'body { font-family: "Foo"; margin: 2% }',
        )

        chapter_count_before = EpubDocument.open(path).content_chapter_count()
        normalize_epub(path, options=NormalizeOptions())
        doc = EpubDocument.open(path)

        assert doc.content_chapter_count() == chapter_count_before

    def test_second_run_is_a_no_op_after_a_font_removal(self, build_epub):
        """Second run produces byte-identical output (TR-NORM-5 contract).

        After the first sweep removes the font, a second run should be a no-op with
        identical bytes.
        """
        path = build_epub([("Chapter 1", "https://example.test/1")])

        _add_font(path, "f1", "OEBPS/foo.ttf")
        _set_css(
            path,
            '@font-face { font-family: "Foo"; src: url(foo.ttf); }\n'
            'body { font-family: "Foo"; margin: 2% }',
        )

        normalize_epub(path, options=NormalizeOptions())
        bytes_after_first = path.read_bytes()

        normalize_epub(path, options=NormalizeOptions())
        bytes_after_second = path.read_bytes()

        assert bytes_after_first == bytes_after_second

    def test_report_counts_the_font_removal_as_a_change(self, build_epub):
        """Font removal counts as a change in the report.

        report.changed is True and report.counts.touched is True.
        """
        path = build_epub([("Chapter 1", "https://example.test/1")])

        _add_font(path, "f1", "OEBPS/foo.ttf")
        _set_css(
            path,
            '@font-face { font-family: "Foo"; src: url(foo.ttf); }\n'
            'body { font-family: "Foo"; margin: 2% }',
        )

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.changed is True
        assert report.counts.touched is True

    def test_debug_summary_is_logged(self, build_epub, caplog):
        """A DEBUG summary is logged at the end of normalization.

        The log message contains "Normalize finished" and the book's filename.
        """
        path = build_epub([("Chapter 1", "https://example.test/1")])

        _add_font(path, "f1", "OEBPS/foo.ttf")
        _set_css(
            path,
            '@font-face { font-family: "Foo"; src: url(foo.ttf); }\n'
            'body { font-family: "Foo"; margin: 2% }',
        )

        with caplog.at_level(logging.DEBUG):
            normalize_epub(path, options=NormalizeOptions())

        assert any(
            "Normalize finished" in record.message and path.name in record.message
            for record in caplog.records
        )

    def test_summary_is_logged_even_for_a_clean_book(self, build_epub, caplog):
        """DEBUG summary is logged even when no changes are made.

        A clean book still produces the "Normalize finished" DEBUG record (a silent
        run is a defect).
        """
        path = build_epub([("Chapter 1", "https://example.test/1")])

        with caplog.at_level(logging.DEBUG):
            normalize_epub(path, options=NormalizeOptions())

        assert any(
            "Normalize finished" in record.message and path.name in record.message
            for record in caplog.records
        )
