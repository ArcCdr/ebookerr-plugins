"""Tests for EPUB normalization against captured real EPUB fixtures.

Safety net against the riskiest property of the normalization feature:
"works on any valid EPUB 2 or 3 file and never makes it worse". Tests
every captured fixture (discovered at collection time) against invariants:
- Normalizer doesn't raise on any real EPUB
- Book remains openable after normalization
- Chapter count is unchanged
- Spine/manifest structure is preserved (fonts may be removed)
- Metadata (title, creator, language) is unchanged
- Cover image is unchanged
- Every chapter remains readable
- Second pass is byte-identical (idempotency)
- No font-family rules survive in stylesheets
- No warnings logged during normalization
- Normalization is logged as completed
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import pytest
import tinycss2
from ebookerr_sdk.epub.document import EpubDocument
from epub_normalize.normalize import (
    NormalizeOptions,
    normalize_epub,
)

# Discover all .epub fixtures at collection time so parametrize can name them
_FIXTURES = sorted((Path(__file__).resolve().parent / "fixtures").glob("*.epub"))


@pytest.fixture
def book(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """Copy one captured fixture EPUB into tmp_path so the test can mutate it.

    Args:
        request: pytest FixtureRequest with param being the fixture path.
        tmp_path: Temporary directory provided by pytest.

    Returns:
        Path to the copied EPUB file in tmp_path.
    """
    src = request.param
    dst = tmp_path / src.name
    shutil.copy2(src, dst)
    return dst


class TestNormalizeFixtures:
    """Tests for EPUB normalization against captured real EPUB fixtures."""

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_normalize_does_not_raise(self, book: Path) -> None:
        """normalize_epub returns a NormalizeReport without raising.

        Args:
            book: The copied EPUB fixture.
        """
        # Capture bytes before normalization
        bytes_before = book.read_bytes()

        report = normalize_epub(book, options=NormalizeOptions())
        assert report is not None

        # If skipped (fixed-layout), the book must be left byte-identical
        if report.skipped_reason is not None:
            bytes_after = book.read_bytes()
            assert bytes_after == bytes_before

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_book_still_opens(self, book: Path) -> None:
        """After normalization, EpubDocument.open succeeds.

        Args:
            book: The copied EPUB fixture.
        """
        normalize_epub(book, options=NormalizeOptions())
        doc = EpubDocument.open(book)
        assert doc is not None

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_chapter_count_is_unchanged(self, book: Path) -> None:
        """Chapter count is the same before and after normalization.

        Args:
            book: The copied EPUB fixture.
        """
        # Capture count before normalization
        doc_before = EpubDocument.open(book)
        count_before = doc_before.content_chapter_count()

        # Normalize
        normalize_epub(book, options=NormalizeOptions())

        # Verify count after
        doc_after = EpubDocument.open(book)
        count_after = doc_after.content_chapter_count()

        assert count_after == count_before

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_spine_and_manifest_hrefs_are_preserved_except_fonts(self, book: Path) -> None:
        """Spine and manifest hrefs are preserved, except fonts may be removed.

        Font suffixes that may disappear: .eot, .otf, .ttc, .ttf, .woff, .woff2
        Spine idref list must be identical.

        Args:
            book: The copied EPUB fixture.
        """
        # Capture manifest and spine before
        doc_before = EpubDocument.open(book)
        hrefs_before = {item.href for item in doc_before.opf.manifest()}
        spine_idrefs_before = [entry.idref for entry in doc_before.opf.spine()]

        # Normalize
        normalize_epub(book, options=NormalizeOptions())

        # Verify after
        doc_after = EpubDocument.open(book)
        hrefs_after = {item.href for item in doc_after.opf.manifest()}
        spine_idrefs_after = [entry.idref for entry in doc_after.opf.spine()]

        # Spine idrefs must be identical
        assert spine_idrefs_after == spine_idrefs_before

        # Hrefs: only font files may disappear
        font_suffixes = {".eot", ".otf", ".ttc", ".ttf", ".woff", ".woff2"}
        removed_hrefs = hrefs_before - hrefs_after
        for href in removed_hrefs:
            href_lower = href.lower()
            assert any(href_lower.endswith(suffix) for suffix in font_suffixes), (
                f"Non-font href '{href}' was removed"
            )

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_metadata_is_preserved(self, book: Path) -> None:
        """Metadata (title, creator, language) is unchanged.

        Args:
            book: The copied EPUB fixture.
        """
        # Capture metadata before
        doc_before = EpubDocument.open(book)
        title_before = doc_before.opf.get_title()
        creator_before = doc_before.opf.get_creator()
        language_before = doc_before.opf.get_language()

        # Normalize
        normalize_epub(book, options=NormalizeOptions())

        # Verify after
        doc_after = EpubDocument.open(book)
        title_after = doc_after.opf.get_title()
        creator_after = doc_after.opf.get_creator()
        language_after = doc_after.opf.get_language()

        assert title_after == title_before
        assert creator_after == creator_before
        assert language_after == language_before

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_cover_is_preserved(self, book: Path) -> None:
        """Cover image is the same before and after (or None both times).

        Args:
            book: The copied EPUB fixture.
        """
        # Capture cover before
        doc_before = EpubDocument.open(book)
        cover_before = doc_before.cover_image()

        # Normalize
        normalize_epub(book, options=NormalizeOptions())

        # Verify after
        doc_after = EpubDocument.open(book)
        cover_after = doc_after.cover_image()

        if cover_before is None:
            assert cover_after is None
        else:
            assert cover_after is not None
            assert cover_after[0] == cover_before[0]  # bytes
            assert cover_after[1] == cover_before[1]  # media_type

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_every_chapter_is_still_readable(self, book: Path) -> None:
        """Every chapter (spine item) remains readable after normalization.

        Args:
            book: The copied EPUB fixture.
        """
        # Normalize first
        normalize_epub(book, options=NormalizeOptions())

        # Verify every chapter is readable
        doc = EpubDocument.open(book)
        for entry in doc.opf.spine():
            item = doc.opf.item_by_id(entry.idref)
            if item is not None:
                data = doc.resource_bytes(item.href)
                assert len(data) > 0, f"Chapter '{item.href}' returned empty bytes"

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_second_pass_is_byte_identical(self, book: Path) -> None:
        """Second pass produces byte-identical output (FR-NORM-13 / TR-NORM-5).

        Args:
            book: The copied EPUB fixture.
        """
        options = NormalizeOptions()

        # First pass
        normalize_epub(book, options=options)
        bytes_after_first = book.read_bytes()

        # Second pass
        report2 = normalize_epub(book, options=options)
        bytes_after_second = book.read_bytes()

        # Bytes must be identical
        assert bytes_after_first == bytes_after_second
        # Second pass must not have made changes
        assert report2.changed is False

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_no_font_family_survives_in_any_stylesheet(self, book: Path) -> None:
        """No font-family rules survive in stylesheets (except preserved rules).

        Preserved rule selectors match: ::first-letter, ::first-line, drop-cap,
        initial-cap, first-letter, svg, image, math.

        Args:
            book: The copied EPUB fixture.
        """
        # Normalize
        normalize_epub(book, options=NormalizeOptions())

        # Check every CSS file
        doc = EpubDocument.open(book)
        preserved_selector_pattern = re.compile(
            r"::?first-letter|::?first-line|drop-?cap|initial-?cap|first-?letter|\bsvg\b|\bimage\b|\bmath\b",
            re.IGNORECASE,
        )

        for item in doc.opf.manifest():
            if not item.href.lower().endswith(".css"):
                continue

            css_bytes = doc.resource_bytes(item.href)
            css_text = css_bytes.decode("utf-8", errors="replace")

            # Parse stylesheet
            rules = tinycss2.parse_stylesheet(css_text)

            for rule in rules:
                # Check only qualified rules (not at-rules)
                if rule.type != "qualified-rule":
                    continue

                # Serialize the prelude (selector)
                prelude_text = tinycss2.serialize(rule.prelude)

                # Skip if it matches preserved pattern
                if preserved_selector_pattern.search(prelude_text):
                    continue

                # Check declarations in this rule
                for decl in rule.content:
                    if decl.type == "declaration" and decl.lower_name == "font-family":
                        raise AssertionError(
                            f"Found font-family in {item.href} under selector '{prelude_text}'"
                        )

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_no_warning_is_logged(self, book: Path, caplog: pytest.LogCaptureFixture) -> None:
        """No warnings are logged from epub_normalize loggers.

        A real, valid book must not trip well-formedness revert or fixed-layout
        bail-out warnings.

        Args:
            book: The copied EPUB fixture.
            caplog: pytest's log capture fixture.
        """
        with caplog.at_level(logging.WARNING):
            normalize_epub(book, options=NormalizeOptions())

        # Filter to only our loggers
        warnings = [
            r
            for r in caplog.records
            if r.name.startswith("epub_normalize.normalize") and r.levelno >= logging.WARNING
        ]
        assert len(warnings) == 0, f"Unexpected warnings: {[r.message for r in warnings]}"

    @pytest.mark.parametrize("book", _FIXTURES, ids=lambda p: p.stem, indirect=True)
    def test_report_is_reported(self, book: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Normalization completion is logged at DEBUG level.

        Args:
            book: The copied EPUB fixture.
            caplog: pytest's log capture fixture.
        """
        with caplog.at_level(logging.DEBUG):
            normalize_epub(book, options=NormalizeOptions())

        # Look for "Normalize finished" message from epub_normalize loggers
        debug_records = [
            r
            for r in caplog.records
            if r.name.startswith("epub_normalize.normalize") and r.levelno >= logging.DEBUG
        ]
        assert any("Normalize finished" in r.message for r in debug_records), (
            f"No 'Normalize finished' log found. Records: {[r.message for r in debug_records]}"
        )
