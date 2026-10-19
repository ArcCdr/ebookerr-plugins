"""Tests for the EPUB normalize orchestrator and CSS-pass pipeline."""

from __future__ import annotations

import dataclasses
import logging
import zipfile
from pathlib import Path

import pytest
import tinycss2
from ebookerr_sdk.epub.document import EpubDocument
from epub_normalize.normalize import (
    NormalizeOptions,
    NormalizeReport,
    normalize_epub,
    options_for_level,
    options_signature,
)
from epub_normalize.normalize.markers import read_css_marker


def _set_css(path: Path, css: str) -> None:
    """Set the CSS content of an EPUB's stylesheet without preserving structure."""
    doc = EpubDocument.open(path)
    doc.write_resource("OEBPS/stylesheet.css", css.encode("utf-8"))
    doc.save()


class TestCssNormalization:
    """Tests for CSS normalization in the EPUB normalize orchestrator."""

    def test_stylesheet_is_normalized(self, build_epub):
        """CSS with font/color is normalized while keeping valid properties."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(
            path,
            "p { font-family: Georgia; color: #222; margin: 2% }",
        )

        report = normalize_epub(path, options=NormalizeOptions())

        doc = EpubDocument.open(path)
        stylesheet = doc.resource_bytes("OEBPS/stylesheet.css").decode("utf-8")

        assert "Georgia" not in stylesheet
        assert "#222" not in stylesheet
        assert "margin" in stylesheet
        assert report.counts.css_files_changed == 1
        assert report.counts.declarations_removed == 2
        assert report.changed is True

    def test_marker_is_stamped_on_a_changed_stylesheet(self, build_epub):
        """A changed stylesheet gets stamped with the marker on its first line."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(
            path,
            "body { font-family: Georgia; color: #222; margin: 2% }",
        )

        options = NormalizeOptions()
        normalize_epub(path, options=options)

        doc = EpubDocument.open(path)
        stylesheet = doc.resource_bytes("OEBPS/stylesheet.css").decode("utf-8")

        marker = read_css_marker(stylesheet)
        assert marker == options_signature(options)
        assert stylesheet.startswith(f"/* ebookerr-normalized:{marker} */")

    def test_second_run_skips_by_marker(self, build_epub):
        """A second run with same options skips the file via marker."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(
            path,
            "body { font-family: Georgia; color: #222; margin: 2% }",
        )

        options = NormalizeOptions()
        normalize_epub(path, options=options)
        report2 = normalize_epub(path, options=options)

        assert report2.counts.files_skipped_marker == 1
        assert report2.counts.css_files_changed == 0
        assert report2.changed is False

    def test_second_run_leaves_the_file_byte_identical(self, build_epub):
        """A second run produces byte-identical output (TR-NORM-5 contract)."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(
            path,
            "body { font-family: Georgia; color: #222; margin: 2% }",
        )

        options = NormalizeOptions()
        normalize_epub(path, options=options)
        bytes_after_first = path.read_bytes()

        normalize_epub(path, options=options)
        bytes_after_second = path.read_bytes()

        assert bytes_after_first == bytes_after_second

    def test_changing_an_option_invalidates_the_marker(self, build_epub):
        """Changing options invalidates the marker and re-stamps with new signature."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        # Use CSS with margins to test that normalize_margins changes the output
        _set_css(path, "p { margin: 10px; padding: 5px }")

        options1 = dataclasses.replace(NormalizeOptions(), normalize_margins=False)
        normalize_epub(path, options=options1)

        options2 = dataclasses.replace(options1, normalize_margins=True)
        report2 = normalize_epub(path, options=options2)

        assert report2.counts.files_skipped_marker == 0

        doc = EpubDocument.open(path)
        stylesheet = doc.resource_bytes("OEBPS/stylesheet.css").decode("utf-8")
        marker = read_css_marker(stylesheet)
        assert marker == options_signature(options2)
        assert marker != options_signature(options1)

    def test_clean_stylesheet_is_not_written(self, build_epub):
        """A clean stylesheet (no changes) is not written."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(path, "p { margin: 2% }")

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.counts.css_files_changed == 0
        assert report.changed is False

        doc = EpubDocument.open(path)
        stylesheet = doc.resource_bytes("OEBPS/stylesheet.css").decode("utf-8")
        assert read_css_marker(stylesheet) is None

    def test_unchanged_book_is_not_saved(self, build_epub):
        """A book with no changes is not saved to disk."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(path, "p { margin: 2% }")

        bytes_before = path.read_bytes()
        normalize_epub(path, options=NormalizeOptions())
        bytes_after = path.read_bytes()

        assert bytes_before == bytes_after

    def test_fixed_layout_book_is_skipped(self, build_epub):
        """Fixed-layout books (pre-paginated or fixed-layout meta) are skipped."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(path, "body { color: red; margin: 2% }")

        # Inject fixed-layout via rendition:layout using EpubDocument
        from ebookerr_sdk.epub.opf import PackageDocument

        doc = EpubDocument.open(path)
        opf_xml = doc.opf.to_xml()
        opf_xml = opf_xml.replace(
            "</metadata>",
            '<meta property="rendition:layout">pre-paginated</meta></metadata>',
        )
        # Re-parse the modified OPF
        doc.opf = PackageDocument.parse(opf_xml)
        doc.save()

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.skipped_reason == "fixed-layout"
        assert report.changed is False

        # Stylesheet should be untouched
        doc = EpubDocument.open(path)
        stylesheet = doc.resource_bytes("OEBPS/stylesheet.css").decode("utf-8")
        assert "color: red" in stylesheet

    def test_fixed_layout_book_is_skipped_via_meta_tag(self, build_epub):
        """Fixed-layout via meta name='fixed-layout' content='true' is also detected."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(path, "body { color: red; margin: 2% }")

        # Inject via meta tag using EpubDocument
        from ebookerr_sdk.epub.opf import PackageDocument

        doc = EpubDocument.open(path)
        opf_xml = doc.opf.to_xml()
        opf_xml = opf_xml.replace(
            "</metadata>",
            '<meta name="fixed-layout" content="true"/></metadata>',
        )
        doc.opf = PackageDocument.parse(opf_xml)
        doc.save()

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.skipped_reason == "fixed-layout"
        assert report.changed is False

    def test_missing_stylesheet_member_is_tolerated(self, build_epub, caplog):
        """Missing stylesheet members are tolerated (manifest mismatch)."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        doc = EpubDocument.open(path)
        doc.remove_member("OEBPS/stylesheet.css")
        doc.save()

        with caplog.at_level(logging.DEBUG):
            report = normalize_epub(path, options=NormalizeOptions())

        assert report.changed is False
        assert "Normalize skipped missing stylesheet" in caplog.text

    def test_css_detected_by_extension_when_media_type_lies(self, build_epub):
        """CSS is detected by .css extension even with wrong media type."""
        path = build_epub([("Chapter 1", "https://example.test/1")])

        # Update main CSS to have something to normalize
        _set_css(path, "body { color: #333; margin: 2% }")

        # Write directly using zipfile to bypass EpubDocument's manifest knowledge
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("OEBPS/extra.css", "p { color: red; margin: 5px }")

        # Manually add to OPF manifest with wrong media type by editing the XML
        from ebookerr_sdk.epub.opf import PackageDocument

        doc = EpubDocument.open(path)
        opf_xml = doc.opf.to_xml()
        # Find and replace - accounting for potential whitespace variations
        import re

        opf_xml = re.sub(
            r'(<item id="style" href="OEBPS/stylesheet\.css"'
            r' media-type="text/css" />)',
            r'\1\n\t\t<item id="extra" href="OEBPS/extra.css"'
            r' media-type="application/octet-stream" />',
            opf_xml,
        )
        # Parse and replace the OPF in the archive
        doc.opf = PackageDocument.parse(opf_xml)
        doc.save()

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.counts.css_files_changed == 2

        doc = EpubDocument.open(path)
        extra_css = doc.resource_bytes("OEBPS/extra.css").decode("utf-8")
        assert "color: red" not in extra_css
        # But margin should remain since strip_colors doesn't affect margin
        assert "margin" in extra_css

    def test_two_stylesheets_both_processed(self, build_epub):
        """Multiple CSS files are all processed."""
        path = build_epub([("Chapter 1", "https://example.test/1")])

        # Update main CSS to have something to normalize
        _set_css(path, "body { color: #333; margin: 2% }")

        # Write directly using zipfile
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("OEBPS/extra.css", "p { color: red; margin: 5px }")

        # Add to OPF manifest
        from ebookerr_sdk.epub.opf import PackageDocument

        doc = EpubDocument.open(path)
        opf_xml = doc.opf.to_xml()
        import re

        opf_xml = re.sub(
            r'(<item id="style" href="OEBPS/stylesheet\.css" media-type="text/css" />)',
            r'\1\n\t\t<item id="extra" href="OEBPS/extra.css" media-type="text/css" />',
            opf_xml,
        )
        doc.opf = PackageDocument.parse(opf_xml)
        doc.save()

        report = normalize_epub(path, options=NormalizeOptions())

        assert report.counts.css_files_changed == 2

    def test_debug_logs_the_rewritten_stylesheet(self, build_epub, caplog):
        """Changed stylesheets are logged at DEBUG level."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(path, "body { font-family: Georgia; color: #222; margin: 2% }")

        with caplog.at_level(logging.DEBUG):
            normalize_epub(path, options=NormalizeOptions())

        assert "Normalize rewrote stylesheet" in caplog.text
        assert "OEBPS/stylesheet.css" in caplog.text

    def test_malformed_epub_propagates(self, tmp_path):
        """Malformed EPUBs raise an exception (does not silent-catch)."""
        import zipfile

        bad_epub = tmp_path / "not-an-epub.epub"
        bad_epub.write_bytes(b"not a zip")

        with pytest.raises(zipfile.BadZipFile):
            normalize_epub(bad_epub, options=NormalizeOptions())


class TestPageTemplates:
    """Adobe page templates are removed with the page layout (FR-NORM-17)."""

    _ITEM = ("pt", "OEBPS/page-template.xpgt", "application/vnd.adobe-page-template+xml")

    def _with_template(self, build_epub) -> Path:
        """Build a book carrying a page-template manifest item, its member and a chapter link."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        doc = EpubDocument.open(path)
        doc.opf.add_manifest_item(*self._ITEM)
        doc.write_resource(
            self._ITEM[1], b"<ade:template xmlns:ade='http://ns.adobe.com/2006/ade'/>"
        )
        chapter = doc.resource_bytes("OEBPS/file0001.xhtml").decode("utf-8")
        chapter = chapter.replace(
            "</head>",
            '<link href="page-template.xpgt" rel="stylesheet" '
            'type="application/vnd.adobe-page-template+xml"/>\n</head>',
        )
        doc.write_resource("OEBPS/file0001.xhtml", chapter.encode("utf-8"))
        doc.save()
        return path

    def test_page_templates_are_removed_at_standard(self, build_epub):
        """At Standard the manifest item, its member and the chapter link are all gone."""
        path = self._with_template(build_epub)

        report = normalize_epub(path, options=options_for_level("standard"))

        doc = EpubDocument.open(path)
        assert doc.opf.item_by_id("pt") is None
        assert doc.member_bytes("OEBPS/page-template.xpgt") is None
        assert b"xpgt" not in doc.resource_bytes("OEBPS/file0001.xhtml")
        assert report.counts.page_templates_removed == 1
        assert report.counts.page_template_links_removed == 1

    def test_page_templates_stay_at_light(self, build_epub):
        """At Light the item, the member and the link all stay."""
        path = self._with_template(build_epub)

        report = normalize_epub(path, options=options_for_level("light"))

        doc = EpubDocument.open(path)
        assert doc.opf.item_by_id("pt") is not None
        assert doc.member_bytes("OEBPS/page-template.xpgt") is not None
        assert b"page-template.xpgt" in doc.resource_bytes("OEBPS/file0001.xhtml")
        assert report.counts.page_templates_removed == 0

    def test_a_template_is_found_by_its_extension_when_the_media_type_lies(self, build_epub):
        """A .xpgt item labelled with another media type is a page template too."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        doc = EpubDocument.open(path)
        doc.opf.add_manifest_item("pt", "OEBPS/page-template.xpgt", "text/xml")
        doc.write_resource("OEBPS/page-template.xpgt", b"<t/>")
        doc.save()

        report = normalize_epub(path, options=options_for_level("standard"))

        assert report.counts.page_templates_removed == 1
        assert EpubDocument.open(path).opf.item_by_id("pt") is None


class TestNormalizeReport:
    """Tests for the NormalizeReport data class."""

    def test_report_changed_is_false_when_only_files_were_skipped(self):
        """NormalizeReport.changed is False when only marker-skipped files exist."""
        from epub_normalize.normalize.counts import NormalizeCounts

        report = NormalizeReport(NormalizeCounts(files_skipped_marker=3))
        assert report.changed is False

    def test_report_changed_is_false_when_skipped(self):
        """NormalizeReport.changed is False when skipped_reason is set."""
        from epub_normalize.normalize.counts import NormalizeCounts

        report = NormalizeReport(
            NormalizeCounts(css_files_changed=1),
            skipped_reason="fixed-layout",
        )
        assert report.changed is False


class TestPackageReexports:
    """Test that the package re-exports the public API."""

    def test_package_reexports(self):
        """NormalizeReport and normalize_epub are re-exported from the package."""
        from epub_normalize.normalize import NormalizeReport, normalize_epub

        assert callable(normalize_epub)
        assert NormalizeReport is not None


class TestSurveyBeforeRewrite:
    """The book survey runs before the stylesheet is rewritten (TR-NORM-7)."""

    def test_the_survey_finds_the_body_text_size(self, build_epub):
        """Ten 11pt body paragraphs make 11pt the base, so the paragraph rule goes."""
        path = build_epub([("Chapter 1", "https://example.test/1")])
        _set_css(path, "p { font-size: 11pt } h1 { font-size: 20pt }")
        paragraphs = "".join("<p>Body text.</p>" for _ in range(10))
        chapter = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
            "<title>Chapter 1</title>\n</head>\n"
            '<body class="fff_chapter">\n<h1>Chapter 1</h1>\n'
            f"{paragraphs}\n</body>\n</html>\n"
        )
        doc = EpubDocument.open(path)
        doc.write_resource("OEBPS/file0001.xhtml", chapter.encode("utf-8"))
        doc.save()

        normalize_epub(path, options=NormalizeOptions())

        stylesheet = EpubDocument.open(path).resource_bytes("OEBPS/stylesheet.css").decode("utf-8")
        rules = [
            r
            for r in tinycss2.parse_stylesheet(stylesheet, skip_comments=True, skip_whitespace=True)
            if r.type == "qualified-rule"
        ]
        assert [tinycss2.serialize(r.prelude).strip() for r in rules] == ["h1"]
        decls = tinycss2.parse_blocks_contents(
            rules[0].content, skip_comments=True, skip_whitespace=True
        )
        assert {d.lower_name: tinycss2.serialize(d.value).strip() for d in decls} == {
            "font-size": "1.818em"
        }
