"""Tests for the EPUB normalize orchestrator and XHTML-pass (chapter) pipeline."""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from ebookerr_sdk.epub.document import EpubDocument

from epub_normalize.normalize import (
    NormalizeOptions,
    normalize_epub,
    options_signature,
)
from epub_normalize.normalize.markers import read_xhtml_marker


def _set_chapter(path: Path, href: str, xhtml: str) -> None:
    """Set the XHTML content of a chapter without preserving structure.

    Args:
        path: Path to the EPUB file.
        href: The manifest href of the chapter to update.
        xhtml: The new XHTML content.
    """
    doc = EpubDocument.open(path)
    doc.write_resource(href, xhtml.encode("utf-8"))
    doc.save()


class TestXhtmlNormalization:
    """Tests for XHTML (chapter) normalization in the EPUB normalize orchestrator."""

    def test_style_block_in_a_chapter_is_normalized(self, build_epub):
        """A <style> block in chapter head normalized (colors removed, properties kept)."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            "<style>p { font-family: Georgia; margin: 0 }</style>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            "<p>Body.</p>\n</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        report = normalize_epub(path, options=NormalizeOptions())

        doc = EpubDocument.open(path)
        chapter = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")

        assert "Georgia" not in chapter
        assert "margin" in chapter
        assert report.counts.xhtml_files_changed == 1
        assert report.counts.declarations_removed == 1

    def test_inline_style_attribute_is_normalized(self, build_epub):
        """A style="" attribute normalized (colors removed, properties kept)."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            '<p style="color:red; margin:0">Body.</p>\n'
            "</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        report = normalize_epub(path, options=NormalizeOptions())

        doc = EpubDocument.open(path)
        chapter = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")

        assert 'style="margin:0"' in chapter or "margin:0" in chapter
        assert "red" not in chapter
        assert report.counts.xhtml_files_changed == 1

    def test_inline_style_attribute_is_removed_when_it_empties(self, build_epub):
        """A style="" attribute that empties after normalization is completely removed."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            '<p class="x" style="color:red">Body.</p>\n'
            "</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        report = normalize_epub(path, options=NormalizeOptions())

        doc = EpubDocument.open(path)
        chapter = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")

        assert '<p class="x">Body.</p>' in chapter
        assert "style" not in chapter.split('<p class="x">')[1].split("</p>")[0]
        assert report.counts.style_attributes_removed == 1
        assert report.counts.xhtml_files_changed == 1

    def test_viewport_meta_is_removed_from_a_chapter(self, build_epub):
        """A <meta name="viewport"> tag in a chapter head is removed."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            '<meta name="viewport" content="width=1200, height=1600"/>\n'
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            "<p>Body.</p>\n</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        report = normalize_epub(path, options=NormalizeOptions())

        doc = EpubDocument.open(path)
        chapter = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")

        assert "viewport" not in chapter
        assert report.counts.viewport_metas_removed == 1
        assert report.counts.xhtml_files_changed == 1

    def test_marker_is_stamped_before_head_close(self, build_epub):
        """After a changed run, the chapter text contains the marker immediately before </head>."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            "<style>p { font-family: Georgia; margin: 0 }</style>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            "<p>Body.</p>\n</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        options = NormalizeOptions()
        normalize_epub(path, options=options)

        doc = EpubDocument.open(path)
        chapter = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")

        assert "<!--ebookerr-normalized:" in chapter
        assert chapter.find("<!--ebookerr-normalized:") < chapter.find("</head>")

        marker = read_xhtml_marker(chapter)
        assert marker == options_signature(options)

    def test_second_run_skips_chapters_by_marker(self, build_epub):
        """A second run with same options skips chapters via marker."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            "<style>p { font-family: Georgia; margin: 0 }</style>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            "<p>Body.</p>\n</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        options = NormalizeOptions()
        normalize_epub(path, options=options)
        report2 = normalize_epub(path, options=options)

        assert report2.counts.files_skipped_marker >= 1
        assert report2.counts.xhtml_files_changed == 0

    def test_second_run_leaves_the_file_byte_identical(self, build_epub):
        """A second run produces byte-identical output (TR-NORM-5 contract)."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            "<style>p { font-family: Georgia; margin: 0 }</style>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            "<p>Body.</p>\n</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        options = NormalizeOptions()
        normalize_epub(path, options=options)
        bytes_after_first = path.read_bytes()

        normalize_epub(path, options=options)
        bytes_after_second = path.read_bytes()

        assert bytes_after_first == bytes_after_second

    def test_clean_chapter_is_not_written(self, build_epub):
        """A plain build_epub book with no styles beyond stylesheet link is not modified."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        report = normalize_epub(path, options=NormalizeOptions())

        doc = EpubDocument.open(path)
        chapter = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")

        assert report.counts.xhtml_files_changed == 0
        assert read_xhtml_marker(chapter) is None

    def test_svg_member_is_never_touched(self, build_epub):
        """SVG members are deliberately excluded and never touched (FR-NORM-10)."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        # Add an SVG member to the EPUB and add to manifest/archive manually
        import zipfile as zf

        svg_content = '<svg xmlns="http://www.w3.org/2000/svg" style="color:red"></svg>'

        # Read the current EPUB
        with zf.ZipFile(path, "r") as archive:
            members = {name: archive.read(name) for name in archive.namelist()}

        # Add the SVG
        members["OEBPS/title.svg"] = svg_content.encode("utf-8")

        # Update the OPF manifest
        opf_text = members["content.opf"].decode("utf-8")
        new_manifest_line = (
            '<item id="title_svg" href="OEBPS/title.svg" media-type="image/svg+xml"/>\n\t\t'
        )
        opf_text = opf_text.replace("\t</manifest>", f"\t\t{new_manifest_line}</manifest>")
        members["content.opf"] = opf_text.encode("utf-8")

        # Rewrite the EPUB
        with zf.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)

        # Run normalization
        normalize_epub(path, options=NormalizeOptions())

        # Verify SVG is unchanged
        with zf.ZipFile(path, "r") as archive:
            result_svg = archive.read("OEBPS/title.svg")

        assert result_svg == svg_content.encode("utf-8")

    def test_missing_chapter_member_is_tolerated(self, build_epub, caplog):
        """A missing chapter member (manifest row without archive entry) is tolerated."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        # Read the current EPUB
        with zipfile.ZipFile(path, "r") as archive:
            members = {name: archive.read(name) for name in archive.namelist()}

        # Remove the chapter from archive but keep manifest entry
        del members["OEBPS/file0001.xhtml"]

        # Rewrite the EPUB
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)

        with caplog.at_level(logging.DEBUG):
            normalize_epub(path, options=NormalizeOptions())

        assert "Normalize skipped missing chapter" in caplog.text

    def test_css_and_chapters_are_both_counted(self, build_epub):
        """A book with both CSS and chapter styles is counted in both categories."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        # Set CSS to have a color
        doc = EpubDocument.open(path)
        doc.write_resource("OEBPS/stylesheet.css", "body { color: red; }")
        doc.save()

        # Set chapter to have an inline color
        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            '<p style="color:blue">Body.</p>\n'
            "</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.counts.css_files_changed == 1
        assert report.counts.xhtml_files_changed == 1

    def test_chapter_detected_by_extension_when_media_type_lies(self, build_epub):
        """A member with .html/.htm extension is still normalized even if media type is wrong."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        # Add an .html file with octet-stream media type
        import zipfile as zf

        html_content = (
            "<html><head><style>p{color:red;margin:0}</style></head><body><p>Test</p></body></html>"
        )

        # Read the current EPUB
        with zf.ZipFile(path, "r") as archive:
            members = {name: archive.read(name) for name in archive.namelist()}

        # Add the HTML file
        members["OEBPS/extra.html"] = html_content.encode("utf-8")

        # Update the OPF manifest
        opf_text = members["content.opf"].decode("utf-8")
        new_manifest_line = (
            '<item id="extra_html" href="OEBPS/extra.html" '
            'media-type="application/octet-stream"/>\n\t\t'
        )
        opf_text = opf_text.replace("\t</manifest>", f"\t\t{new_manifest_line}</manifest>")
        members["content.opf"] = opf_text.encode("utf-8")

        # Rewrite the EPUB
        with zf.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)

        report = normalize_epub(path, options=NormalizeOptions())

        with zf.ZipFile(path, "r") as archive:
            result = archive.read("OEBPS/extra.html").decode("utf-8")

        assert "red" not in result
        assert "margin" in result
        assert report.counts.xhtml_files_changed >= 1

    def test_malformed_chapter_is_still_normalized(self, build_epub):
        """A malformed chapter is normalized without parsing, and the book still opens."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        # Malformed but processable XHTML
        malformed_xhtml = (
            "<html><head><style>p{color:red;margin:0}</style></head><body><p>x</body></html>"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", malformed_xhtml)

        report = normalize_epub(path, options=NormalizeOptions())

        doc = EpubDocument.open(path)
        result = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")

        assert "red" not in result
        assert report.counts.xhtml_files_changed == 1

    def test_book_still_opens_and_keeps_its_chapter_count(self, build_epub):
        """For a three-chapter styled book, after the run the chapter count is unchanged."""
        path = build_epub(
            [
                ("Chapter 1", "https://example.test/1"),
                ("Chapter 2", "https://example.test/2"),
                ("Chapter 3", "https://example.test/3"),
            ],
            include_title_page=False,
        )

        # Add styles to all chapters
        for i in range(1, 4):
            chapter_xhtml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
                f"<title>Chapter {i}</title>\n"
                "<style>p { color: red; margin: 0 }</style>\n"
                '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
                f'<meta name="chapterurl" content="https://example.test/{i}" />\n'
                f'<meta name="chaptertitle" content="Chapter {i}" />\n'
                '</head>\n<body class="fff_chapter">\n'
                f'<h3 class="fff_chapter_title">Chapter {i}</h3>\n'
                "<p>Body.</p>\n</body>\n</html>\n"
            )
            _set_chapter(path, f"OEBPS/file000{i}.xhtml", chapter_xhtml)

        doc_before = EpubDocument.open(path)
        count_before = doc_before.content_chapter_count()

        normalize_epub(path, options=NormalizeOptions())

        doc_after = EpubDocument.open(path)
        count_after = doc_after.content_chapter_count()

        assert count_after == count_before
        assert count_after == 3

    def test_debug_logs_the_rewritten_chapter(self, build_epub, caplog):
        """With DEBUG logging, a changed run logs a record containing the chapter href."""
        path = build_epub(
            [("Chapter 1", "https://example.test/1")],
            include_title_page=False,
        )

        chapter_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n"
            "<style>p { color: red; margin: 0 }</style>\n"
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
            '<meta name="chapterurl" content="https://example.test/1" />\n'
            '<meta name="chaptertitle" content="Chapter 1" />\n'
            '</head>\n<body class="fff_chapter">\n'
            '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
            "<p>Body.</p>\n</body>\n</html>\n"
        )
        _set_chapter(path, "OEBPS/file0001.xhtml", chapter_xhtml)

        with caplog.at_level(logging.DEBUG):
            normalize_epub(path, options=NormalizeOptions())

        assert "Normalize rewrote chapter" in caplog.text
        assert "OEBPS/file0001.xhtml" in caplog.text
