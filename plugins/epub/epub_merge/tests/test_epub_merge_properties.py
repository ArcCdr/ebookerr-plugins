"""Tests for EPUB3 manifest ``properties`` declaration in merged chapters."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from epub_merge.merge.model import InputBook, InputChapter, MergeOptions
from epub_merge.merge.plan import build_merge_plan

if TYPE_CHECKING:
    from _pytest.logging import LogCaptureFixture


def _book(version: str, *chapters_data: tuple[str, bytes]) -> InputBook:
    """Build a neutral InputBook with the given chapters and version.

    Args:
        version: EPUB version ("2.0" or "3.0").
        *chapters_data: Tuples of (label, xhtml_bytes) for each chapter.

    Returns:
        An InputBook with the specified chapters.
    """
    chapters = tuple(
        InputChapter(
            label=label,
            href=f"OEBPS/c{i:04d}.xhtml",
            item_id=f"c{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=xhtml,
        )
        for i, (label, xhtml) in enumerate(chapters_data)
    )
    return InputBook(
        index=0,
        name="book.epub",
        version=version,
        title="Test Book",
        creators=(),
        contributors=(),
        language="en",
        identifier="id-test",
        source=None,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root="OEBPS",
        chapters=chapters,
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )


class TestChapterPropertiesDeclaration:
    """Verify that merged chapters declare svg/mathml/scripted in the manifest."""

    def test_svg_chapter_declares_svg(self) -> None:
        """An EPUB3 chapter containing SVG earns the ``svg`` property (AT-PROP-1)."""
        chapter_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 1</title></head>
  <body>
    <h3 class="fff_chapter_title">Chapter 1</h3>
    <p>Some text</p>
    <svg xmlns="http://www.w3.org/2000/svg"><circle/></svg>
  </body>
</html>"""
        book = _book("3.0", ("Chapter 1", chapter_xhtml))
        plan = build_merge_plan([book], MergeOptions())

        # Find the chapter entry in the manifest
        chapter_entries = [e for e in plan.package.manifest if e.href == "chapter_01.xhtml"]
        assert len(chapter_entries) == 1
        assert chapter_entries[0].properties == "svg"

    def test_mathml_and_script_chapter_declares_both(self) -> None:
        """A chapter with both ``<math>`` and ``<script>`` earns both properties (AT-PROP-2)."""
        chapter_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 1</title></head>
  <body>
    <h3 class="fff_chapter_title">Chapter 1</h3>
    <p>Equation: <math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math></p>
    <script>console.log('test');</script>
  </body>
</html>"""
        book = _book("3.0", ("Chapter 1", chapter_xhtml))
        plan = build_merge_plan([book], MergeOptions())

        chapter_entries = [e for e in plan.package.manifest if e.href == "chapter_01.xhtml"]
        assert len(chapter_entries) == 1
        props = chapter_entries[0].properties.split()
        assert set(props) == {"mathml", "scripted"}

    def test_properties_are_sorted(self) -> None:
        """The ``properties`` attribute is sorted for determinism."""
        chapter_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 1</title></head>
  <body>
    <h3 class="fff_chapter_title">Chapter 1</h3>
    <math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>
    <script>console.log('test');</script>
  </body>
</html>"""
        book = _book("3.0", ("Chapter 1", chapter_xhtml))
        plan = build_merge_plan([book], MergeOptions())

        chapter_entries = [e for e in plan.package.manifest if e.href == "chapter_01.xhtml"]
        assert len(chapter_entries) == 1
        # Sorted order: mathml before scripted
        assert chapter_entries[0].properties == "mathml scripted"

    def test_plain_chapter_has_no_properties(self) -> None:
        """A chapter with no SVG, MathML, or script earns no properties."""
        chapter_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 1</title></head>
  <body>
    <h3 class="fff_chapter_title">Chapter 1</h3>
    <p>Plain text content.</p>
  </body>
</html>"""
        book = _book("3.0", ("Chapter 1", chapter_xhtml))
        plan = build_merge_plan([book], MergeOptions())

        chapter_entries = [e for e in plan.package.manifest if e.href == "chapter_01.xhtml"]
        assert len(chapter_entries) == 1
        assert chapter_entries[0].properties == ""

    def test_epub2_never_sets_properties(self) -> None:
        """EPUB2 output never sets the ``properties`` attribute on chapter items."""
        chapter_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 1</title></head>
  <body>
    <h3 class="fff_chapter_title">Chapter 1</h3>
    <svg xmlns="http://www.w3.org/2000/svg"><circle/></svg>
  </body>
</html>"""
        book = _book("2.0", ("Chapter 1", chapter_xhtml))
        plan = build_merge_plan([book], MergeOptions())

        chapter_entries = [e for e in plan.package.manifest if e.href == "chapter_01.xhtml"]
        assert len(chapter_entries) == 1
        assert chapter_entries[0].properties == ""

    def test_nav_and_cover_properties_unaffected(self, tmp_path) -> None:
        """Special entries (nav, cover-image) retain their properties in EPUB3."""

        chapter_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 1</title></head>
  <body>
    <h3 class="fff_chapter_title">Chapter 1</h3>
    <p>Content</p>
  </body>
</html>"""
        book = _book("3.0", ("Chapter 1", chapter_xhtml))

        # Create a temporary cover image file
        cover_path = tmp_path / "cover.jpg"
        cover_path.write_bytes(b"fake image data")

        options = MergeOptions(cover_image=cover_path)
        plan = build_merge_plan([book], options)

        # Check nav entry has properties="nav"
        nav_entries = [e for e in plan.package.manifest if e.item_id == "nav"]
        assert len(nav_entries) == 1
        assert nav_entries[0].properties == "nav"

        # Check cover-image entry has properties="cover-image"
        cover_entries = [e for e in plan.package.manifest if e.properties == "cover-image"]
        assert len(cover_entries) == 1

    def test_unparseable_chapter_warns_and_declares_nothing(
        self, caplog: LogCaptureFixture
    ) -> None:
        """An unparseable chapter logs a warning and declares no properties."""
        # Malformed XML
        chapter_xhtml = b"<html><body><p>unclosed"
        book = _book("3.0", ("Chapter 1", chapter_xhtml))

        with caplog.at_level(logging.WARNING):
            plan = build_merge_plan([book], MergeOptions())

        chapter_entries = [e for e in plan.package.manifest if e.href == "chapter_01.xhtml"]
        assert len(chapter_entries) == 1
        assert chapter_entries[0].properties == ""

        # Verify warning was logged
        assert any("could not scan" in record.message for record in caplog.records)

    def test_logs_debug_for_declared_properties(self, caplog: LogCaptureFixture) -> None:
        """A chapter with properties logs a DEBUG message."""
        chapter_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 1</title></head>
  <body>
    <h3 class="fff_chapter_title">Chapter 1</h3>
    <svg xmlns="http://www.w3.org/2000/svg"><circle/></svg>
  </body>
</html>"""
        book = _book("3.0", ("Chapter 1", chapter_xhtml))

        with caplog.at_level(logging.DEBUG):
            build_merge_plan([book], MergeOptions())

        # Verify debug log contains the chapter filename and properties
        assert any("declares EPUB3 properties" in record.message for record in caplog.records)
