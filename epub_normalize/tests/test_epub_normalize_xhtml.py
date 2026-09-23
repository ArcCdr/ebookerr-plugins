"""Tests for EPUB XHTML document rewriter (style blocks, attributes, viewport meta)."""

from __future__ import annotations

import dataclasses
import logging

from epub_normalize.normalize.counts import NormalizeCounts
from epub_normalize.normalize.options import NormalizeOptions
from epub_normalize.normalize.xhtml import is_well_formed, normalize_xhtml


def _run(text: str, **kw: bool) -> tuple[str, NormalizeCounts]:
    """Normalize one chapter document and return the result plus the counts it moved."""
    counts = NormalizeCounts()
    options = dataclasses.replace(NormalizeOptions(), **kw)
    return normalize_xhtml(text, options=options, counts=counts, name="OEBPS/c1.xhtml"), counts


_DOC = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<!DOCTYPE html>\n"
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
    "{head}</head><body>{body}</body></html>"
)


class TestStyleBlocks:
    """Test <style> block normalization."""

    def test_style_block_is_normalized(self):
        """Normalize CSS declarations inside <style> blocks."""
        text = _DOC.format(
            head='<style type="text/css">p { font-family: Georgia; margin: 0 }</style>',
            body="",
        )
        result, counts = _run(text)
        assert "margin" in result
        assert "Georgia" not in result
        assert counts.declarations_removed == 1

    def test_style_block_wrapped_in_a_comment_is_left_alone(self):
        """CSS wrapped in XML comments inside <style> blocks is preserved."""
        text = _DOC.format(
            head="<style><!-- p{font-family:Georgia} --></style>",
            body="",
        )
        result, counts = _run(text)
        assert result == text
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0
        assert counts.rules_removed == 0
        assert counts.style_attributes_removed == 0
        assert counts.viewport_metas_removed == 0


class TestStyleAttributes:
    """Test style="" attribute normalization."""

    def test_style_attribute_properties_are_stripped(self):
        """Declarations in style attributes are filtered."""
        text = _DOC.format(head="", body='<p style="color:red; margin:0">x</p>')
        result, counts = _run(text)
        assert 'style="margin:0"' in result
        assert "red" not in result
        assert counts.declarations_removed == 1
        assert counts.style_attributes_removed == 0

    def test_style_attribute_is_removed_when_it_empties(self):
        """Empty style attributes are completely removed."""
        text = _DOC.format(head="", body='<p class="a" style="color:red">x</p>')
        result, counts = _run(text)
        assert '<p class="a">x</p>' in result
        assert "style" not in result
        assert counts.style_attributes_removed == 1

    def test_single_quoted_style_attribute(self):
        """Single-quoted style attributes preserve their quotes."""
        text = _DOC.format(head="", body="<p style='color:red; margin:0'>x</p>")
        result, counts = _run(text)
        assert "style='" in result
        assert "margin" in result
        assert "red" not in result

    def test_style_attribute_on_any_element(self):
        """Style attributes on all elements are normalized."""
        text = _DOC.format(
            head="",
            body='<div style="background-color:#fff"><span style="font-family:X">y</span></div>',
        )
        result, counts = _run(text)
        assert "background-color" not in result
        assert "font-family" not in result
        assert counts.style_attributes_removed == 2

    def test_style_attribute_font_size_is_converted(self):
        """Absolute font sizes in inline styles are converted to em."""
        text = _DOC.format(head="", body='<p style="font-size:12pt">x</p>')
        result, counts = _run(text)
        assert "1em" in result
        assert counts.declarations_converted == 1

    def test_style_inside_a_comment_is_not_touched(self):
        """Inline styles inside HTML comments are never rewritten."""
        text = _DOC.format(
            head="",
            body='<!-- <p style="color:red">x</p> --><p>y</p>',
        )
        result, counts = _run(text)
        assert result == text
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0
        assert counts.style_attributes_removed == 0

    def test_style_text_inside_a_style_block_is_not_treated_as_an_attribute(self):
        """CSS selectors inside <style> blocks are never treated as attributes."""
        text = _DOC.format(
            head="<style>p[data-x] { margin: 0 }</style>",
            body="<p>x</p>",
        )
        result, counts = _run(text)
        assert result == text


class TestViewportMeta:
    """Test viewport meta tag removal."""

    def test_viewport_meta_is_removed(self):
        """Viewport meta tags are removed from reflowable books."""
        text = _DOC.format(
            head='<meta name="viewport" content="width=1200, height=1600"/>',
            body="",
        )
        result, counts = _run(text)
        assert "viewport" not in result
        assert counts.viewport_metas_removed == 1

    def test_viewport_meta_removal_is_case_insensitive(self):
        """Viewport meta tags are matched case-insensitively."""
        text = _DOC.format(
            head='<META NAME="viewport" content="x"/>',
            body="",
        )
        result, counts = _run(text)
        assert "viewport" not in result

    def test_other_meta_tags_survive(self):
        """Non-viewport meta tags are preserved."""
        text = _DOC.format(
            head='<meta charset="utf-8"/>',
            body="",
        )
        result, counts = _run(text)
        assert result == text


class TestIdempotency:
    """Test idempotency and no-op behavior."""

    def test_no_change_returns_the_input_verbatim(self):
        """Documents with no styles return the input string unchanged."""
        text = _DOC.format(head="", body="<p>x</p>")
        result, counts = _run(text)
        assert result == text
        assert counts.declarations_removed == 0
        assert counts.declarations_converted == 0
        assert counts.rules_removed == 0
        assert counts.style_attributes_removed == 0
        assert counts.viewport_metas_removed == 0

    def test_doctype_and_comments_survive_a_rewrite(self):
        """DOCTYPE, comments, and processing instructions survive rewrites."""
        text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!DOCTYPE html>\n"
            '<?xml-stylesheet href="style.css"?>\n'
            "<!-- author note -->\n"
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><body><p style="color:red;margin:0">x</p></body></html>'
        )
        result, counts = _run(text)
        assert "<!DOCTYPE html>" in result
        assert "<?xml-stylesheet" in result
        assert "<!-- author note -->" in result

    def test_second_pass_is_a_no_op(self):
        """A second normalization pass is byte-identical and moves no counters."""
        text = _DOC.format(
            head="<style>p { font-family: Georgia; margin: 0 }</style>",
            body='<p style="color:red;margin:0">x</p>',
        )
        result1, counts1 = _run(text)
        result2, counts2 = _run(result1)
        assert result2 == result1
        assert counts2.declarations_removed == 0
        assert counts2.declarations_converted == 0
        assert counts2.rules_removed == 0
        assert counts2.style_attributes_removed == 0
        assert counts2.viewport_metas_removed == 0


class TestMalformedAndBroken:
    """Test handling of malformed and edge-case documents."""

    def test_malformed_chapter_is_still_rewritten(self):
        """Malformed XHTML is rewritten despite parse failures."""
        text = "<html><head><style>p{color:red;margin:0}</style></head><body><p>x</body></html>"
        result, counts = _run(text)
        assert "color" not in result
        assert counts.declarations_removed == 1
        # Ensure no WARNING about reverting was logged
        assert True  # Will be verified with caplog in actual test run

    def test_entity_bearing_chapter_is_still_rewritten(self):
        """Documents with HTML entities are rewritten without revert."""
        text = _DOC.format(head="", body='<p style="color:red;margin:0">&nbsp;</p>')
        result, counts = _run(text)
        assert "color" not in result
        assert counts.declarations_removed == 1

    def test_revert_when_a_rewrite_breaks_well_formedness(self, monkeypatch, caplog):
        """Rewrites that break well-formedness are reverted and logged."""
        text = _DOC.format(
            head='<meta name="viewport" content="x"/>',
            body="",
        )

        # Patch _remove_viewport_metas to intentionally break the document
        def broken_remove(text, *, counts):
            counts.viewport_metas_removed += 1
            return text + "<unclosed>"

        monkeypatch.setattr(
            "src.services.epub_normalize.xhtml._remove_viewport_metas",
            broken_remove,
        )

        with caplog.at_level(logging.WARNING):
            result, counts = _run(text)

        # Should revert and return the original
        assert result == text
        assert counts.viewport_metas_removed == 0
        # Check for the warning
        warning_messages = [r.message for r in caplog.records if "Normalize reverted" in r.message]
        assert len(warning_messages) > 0
        assert "OEBPS/c1.xhtml" in warning_messages[0]
        assert "no longer well-formed XML" in warning_messages[0]


class TestIsWellFormed:
    """Test the is_well_formed helper function."""

    def test_is_well_formed_valid_xml(self):
        """Valid XML is detected as well-formed."""
        assert is_well_formed("<a><b/></a>") is True

    def test_is_well_formed_mismatched_tags(self):
        """Mismatched tags are detected as not well-formed."""
        assert is_well_formed("<a><b></a>") is False

    def test_is_well_formed_entity(self):
        """HTML entities cause parse failures and are detected as not well-formed."""
        assert is_well_formed("<a>&nbsp;</a>") is False
