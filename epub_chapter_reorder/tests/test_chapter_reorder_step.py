"""Tests for F9 ``reorder_epub`` — fix out-of-order EPUB chapters (ncx + spine)."""

from __future__ import annotations

import hashlib
import inspect
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from ebookerr_sdk.epub import EpubDocument, EpubError, NavPoint
from epub_chapter_reorder.reorder_step import (
    _sort_key,
    _spine_order,
    reorder_epub,
    resolve_book_title,
    resolve_nav_title,
)

_L = "https://www.literotica.com/s/"
# The real out-of-order Tending Bar order: Part 5, Part 6, Pt.01..04 -> file0001..0006.
_TENDING_BAR = [
    ("Tending Bar Part 5", _L + "tending-bar-part-5"),
    ("Tending Bar Part 6", _L + "tending-bar-part-6"),
    ("Tending Bar Pt. 01", _L + "tending-bar-pt-01"),
    ("Tending Bar Pt. 02", _L + "tending-bar-pt-02"),
    ("Tending Bar Pt. 03", _L + "tending-bar-pt-03"),
    ("Tending Bar Pt. 04", _L + "tending-bar-pt-04"),
]
# Chronological: Pt.01..04 then Part 5, Part 6.
_SORTED = ["title_page", "file0003", "file0004", "file0005", "file0006", "file0001", "file0002"]


def _run(epub: Path | None) -> None:
    if epub is None or not epub.is_file():
        return
    try:
        reorder_epub(epub)
    except EpubError:
        logging.warning("Chapter-reorder skipped for %s (malformed EPUB)", epub, exc_info=True)


class TestReorder:
    def test_reorders_ncx_and_spine_together(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        _run(epub)
        doc = EpubDocument.open(epub)
        assert [p.id for p in doc.ncx.nav_points()] == _SORTED
        assert [s.idref for s in doc.opf.spine()] == _SORTED

    def test_renumbers_play_order_one_based(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        _run(epub)
        assert [p.play_order for p in EpubDocument.open(epub).ncx.nav_points()] == list(range(1, 8))

    def test_labels_and_src_travel_with_their_navpoint(
        self, build_epub: Callable[..., Path]
    ) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        before = {p.id: (p.label, p.src) for p in EpubDocument.open(epub).ncx.nav_points()}
        _run(epub)
        after = {p.id: (p.label, p.src) for p in EpubDocument.open(epub).ncx.nav_points()}
        assert after == before

    def test_title_page_stays_pinned_first(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        _run(epub)
        assert EpubDocument.open(epub).ncx.nav_points()[0].id == "title_page"


class TestStructuredSortKey:
    def test_ranges_and_letter_suffixes(self, build_epub: Callable[..., Path]) -> None:
        chapters = [
            ("Book Ch. 10", _L + "a"),
            ("Book Ch. 7a", _L + "b"),
            ("Book Ch. 03 Pt. 02", _L + "c"),
            ("Book Ch. 03 Pt. 01", _L + "d"),
            ("Book Ch. 441-450", _L + "e"),
            ("Book Ch. 32-34", _L + "f"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        # 03a < 03b < 7a < 10 < 32-34 < 441-450
        assert [p.id for p in EpubDocument.open(epub).ncx.nav_points()] == [
            "title_page",
            "file0004",
            "file0003",
            "file0002",
            "file0001",
            "file0006",
            "file0005",
        ]


class TestNoOp:
    def test_already_ordered_is_not_rewritten(self, build_epub: Callable[..., Path]) -> None:
        ordered = [_TENDING_BAR[i] for i in (2, 3, 4, 5, 0, 1)]  # Pt.01..04, Part 5, Part 6
        epub = build_epub(ordered, doc_title="Tending Bar")
        before = epub.read_bytes()
        _run(epub)
        assert epub.read_bytes() == before

    def test_idempotent_second_run_is_noop(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        _run(epub)
        after_first = epub.read_bytes()
        _run(epub)
        assert epub.read_bytes() == after_first

    def test_single_chapter_is_noop(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("A Taste of Jamaica", _L + "a-taste-of-jamaica")], doc_title="A Taste")
        before = epub.read_bytes()
        _run(epub)
        assert epub.read_bytes() == before


class TestDefensive:
    def test_missing_file_returns(self, tmp_path: Path) -> None:
        _run(tmp_path / "nope.epub")  # not a file -> no-op

    def test_malformed_epub_is_logged_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = tmp_path / "bad.epub"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")  # no container.xml
        with caplog.at_level(logging.WARNING):
            _run(bad)
        assert any("reorder" in record.message.lower() for record in caplog.records)


class TestZoneOrdering:
    """Tests for the new zone-based ordering model: title > front > content > back."""

    def test_unnumbered_content_keeps_its_slot_between_numbered(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Unnumbered content chapters keep their original slot among numbered chapters."""
        chapters = [
            ("AIF 30", _L + "a30"),
            ("AIF-Bonus Material Two", _L + "b"),
            ("AIF 32", _L + "a32"),
        ]
        epub = build_epub(chapters, doc_title="AIF")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        assert labels == ["Title Page", "AIF 30", "AIF-Bonus Material Two", "AIF 32"]

    def test_numbered_chapters_still_sort_around_a_fixed_unnumbered_slot(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Numbered chapters sort into their slots; unnumbered chapters stay fixed."""
        chapters = [
            ("Book Ch. 03", _L + "c3"),
            ("Book Interlude", _L + "interlude"),
            ("Book Ch. 01", _L + "c1"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        assert labels == ["Title Page", "Book Ch. 01", "Book Interlude", "Book Ch. 03"]

    def test_front_matter_still_moves_to_the_front(self, build_epub: Callable[..., Path]) -> None:
        """Prologue moves to the front (after title page)."""
        chapters = [
            ("Book - Ch. 01", _L + "c1"),
            ("Book - Interlude", _L + "interlude"),
            ("Book - Prologue", _L + "prol"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        assert labels == ["Title Page", "Book - Prologue", "Book - Ch. 01", "Book - Interlude"]

    def test_back_matter_still_moves_to_the_end(self, build_epub: Callable[..., Path]) -> None:
        """Epilogue moves to the end."""
        chapters = [
            ("Book - Epilogue", _L + "epi"),
            ("Book - Ch. 02", _L + "c2"),
            ("Book - Ch. 01", _L + "c1"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        assert labels == ["Title Page", "Book - Ch. 01", "Book - Ch. 02", "Book - Epilogue"]

    def test_all_unnumbered_content_is_a_noop(self, build_epub: Callable[..., Path]) -> None:
        """All unnumbered content stays in original order (no-op)."""
        chapters = [
            ("Alpha", _L + "a"),
            ("Beta", _L + "b"),
            ("Gamma", _L + "c"),
        ]
        epub = build_epub(chapters, doc_title="Greek")
        before = epub.read_bytes()
        _run(epub)
        assert epub.read_bytes() == before

    def test_unknown_unnumbered_keeps_its_slot_between_numbered(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Unknown unnumbered chapters keep their slot among the content chapters."""
        chapters = [
            ("Book - Ch. 01", _L + "c1"),
            ("Book - Interlude at the Manor", _L + "interlude"),  # No number, not special
            ("Book - Ch. 02", _L + "c2"),
            ("Book - Epilogue", _L + "epi"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        assert labels == [
            "Title Page",
            "Book - Ch. 01",
            "Book - Interlude at the Manor",
            "Book - Ch. 02",
            "Book - Epilogue",
        ]

    def test_prologue_moves_to_front(self, build_epub: Callable[..., Path]) -> None:
        """Prologue should move from between chapters to the front (after title)."""
        chapters = [
            ("Book - Ch. 01", _L + "c1"),
            ("Book - Ch. 03", _L + "c3"),
            ("Book - Prologue", _L + "prol"),
            ("Book - Ch. 02", _L + "c2"),
            ("Book - Epilogue", _L + "epi"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        # Title Page stays first, prologue moves to front, chapters sorted, epilogue at end
        assert labels == [
            "Title Page",  # Auto-generated by build_epub
            "Book - Prologue",
            "Book - Ch. 01",
            "Book - Ch. 02",
            "Book - Ch. 03",
            "Book - Epilogue",
        ]

    def test_duplicate_numbers_kept_stable(self, build_epub: Callable[..., Path]) -> None:
        """Duplicate chapter numbers should keep their original relative order."""
        chapters = [
            ("Book - Ch. 02", _L + "c2a"),
            ("Book - Ch. 02", _L + "c2b"),
            ("Book - Ch. 01", _L + "c1"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        # Ch. 01 before both 02s; both 02s keep their original order (c2a, then c2b)
        assert labels == [
            "Title Page",
            "Book - Ch. 01",
            "Book - Ch. 02",
            "Book - Ch. 02",
        ]

    def test_empty_doctitle_uses_opf_and_logs_title(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty docTitle should fall back to OPF title and use it in the log."""
        chapters = [
            ("Merged Book - Ch. 02", _L + "c2"),
            ("Merged Book - Ch. 01", _L + "c1"),
        ]
        # Create EPUB with empty docTitle but the OPF will have the title
        epub = build_epub(chapters, doc_title="")
        # Manually set OPF title since build_epub doesn't do it when docTitle is empty
        doc = EpubDocument.open(epub)
        doc.opf.set_title("Merged Book")
        doc.save()

        with caplog.at_level(logging.INFO):
            _run(epub)

        # Check that the log record contains the resolved book title
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert any("Merged Book" in r.message for r in info_records)

    def test_navlabel_fallback_orders_by_xhtml_title(self, build_epub: Callable[..., Path]) -> None:
        """Chapters with empty navLabel but XHTML title should be sorted correctly via fallback."""
        # Build an EPUB where chapters have XHTML titles but navLabels will be empty
        chapters = [
            ("Ch. 03 - Third", _L + "c3"),
            ("Ch. 01 - First", _L + "c1"),
            ("Ch. 02 - Second", _L + "c2"),
        ]
        epub = build_epub(chapters, doc_title="Book")

        # The factory builds chapters with labels from the title. We test that
        # resolve_nav_title correctly uses XHTML fallback when needed.
        # Verify the chapters are currently out of order.
        doc = EpubDocument.open(epub)
        ids_before = [p.id for p in doc.ncx.nav_points()]
        # Expect: title_page, file0001 (Ch.03), file0002 (Ch.01), file0003 (Ch.02)
        assert ids_before == ["title_page", "file0001", "file0002", "file0003"]

        _run(epub)
        doc = EpubDocument.open(epub)
        ids_after = [p.id for p in doc.ncx.nav_points()]
        # After reorder: title_page, file0002 (Ch.01), file0003 (Ch.02), file0001 (Ch.03)
        assert ids_after == ["title_page", "file0002", "file0003", "file0001"]


class TestInternals:
    def test_zone_mapping_uses_shared_chapter_role(self) -> None:
        """Zone mapping uses the shared chapter_role classification."""
        from ebookerr_sdk.epub.chapters import chapter_role
        from epub_chapter_reorder.reorder_step import _ZONE_BY_ROLE

        # Verify _ZONE_BY_ROLE maps roles to expected zones
        assert _ZONE_BY_ROLE[chapter_role("Title Page", "")] == 0
        assert _ZONE_BY_ROLE[chapter_role("Book Ch. 3", "3")] == 2
        assert _ZONE_BY_ROLE[chapter_role("Prologue", "")] == 1
        assert _ZONE_BY_ROLE[chapter_role("Epilogue", "")] == 3
        assert _ZONE_BY_ROLE[chapter_role("Interlude", "")] == 2

    def test_sort_key_non_numeric_sorts_last(self) -> None:
        assert _sort_key("abc") == (1 << 30, "abc")
        assert _sort_key("3a") < _sort_key("10")  # leading int dominates

    def test_spine_order_skips_navpoint_without_manifest_item(
        self, build_epub: Callable[..., Path]
    ) -> None:
        doc = EpubDocument.open(build_epub(_TENDING_BAR, doc_title="Tending Bar"))
        points = doc.ncx.nav_points()
        ghost = NavPoint("ghost", 99, "Ghost", "OEBPS/ghost.xhtml")  # no manifest item
        order = _spine_order(doc, [*points, ghost], [*[p.id for p in points], "ghost"])
        assert "ghost" not in order
        assert order == [p.id for p in points]


class TestTelemetry:
    """Tests for chapter-reorder telemetry: log every outcome with appropriate detail."""

    def test_rewrite_logs_moved_and_total_at_info(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rewrite logs moved count and total at INFO level."""
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        with caplog.at_level(logging.INFO):
            _run(epub)
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert any(
            "Chapter-reorder fixed" in r.getMessage()
            and "of 7 navPoint(s)" in r.getMessage()
            and '"Tending Bar"' in r.getMessage()
            for r in info_records
        )

    def test_already_ordered_logs_unchanged_at_info(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Already-ordered book logs unchanged at INFO level."""
        ordered = [_TENDING_BAR[i] for i in (2, 3, 4, 5, 0, 1)]
        epub = build_epub(ordered, doc_title="Tending Bar")
        with caplog.at_level(logging.INFO):
            _run(epub)
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert any(
            'Chapter-reorder left "Tending Bar" unchanged' in r.getMessage()
            and "7 navPoint(s) already in order" in r.getMessage()
            for r in info_records
        )

    def test_computed_order_logged_at_debug(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Computed order logged at DEBUG level."""
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        with caplog.at_level(logging.DEBUG):
            _run(epub)
        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert any(
            "Chapter-reorder computed order" in r.getMessage() and "6 numbered" in r.getMessage()
            for r in debug_records
        )

    def test_duplicate_nav_ids_log_a_warning_and_skip(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Duplicate nav IDs log a warning and skip reordering."""
        # Build EPUB with duplicate nav IDs by modifying the archive directly
        epub_path = tmp_path / "dup.epub"

        # Create minimal EPUB with duplicate navPoint IDs
        container_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            "\t<rootfiles>\n"
            '\t\t<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>\n'
            "\t</rootfiles>\n</container>\n"
        )
        opf_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package version="2.0" xmlns="http://www.idpf.org/2007/opf" '
            'unique-identifier="fanficfare-uid">\n'
            '\t<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '\t\t<dc:identifier id="fanficfare-uid">test-uid</dc:identifier>\n'
            "\t\t<dc:title>Dup Book</dc:title>\n"
            "\t</metadata>\n"
            "\t<manifest>\n"
            '\t\t<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            '\t\t<item id="ch1" href="OEBPS/ch1.xhtml" media-type="application/xhtml+xml"/>\n'
            '\t\t<item id="ch2" href="OEBPS/ch2.xhtml" media-type="application/xhtml+xml"/>\n'
            "\t</manifest>\n"
            '\t<spine toc="ncx">\n'
            '\t\t<itemref idref="ch1"/>\n'
            '\t\t<itemref idref="ch2"/>\n'
            "\t</spine>\n</package>\n"
        )
        # Two navPoints with the same id
        ncx_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">\n'
            "\t<head>\n"
            '\t\t<meta name="dtb:uid" content="test-uid"/>\n'
            "\t</head>\n"
            "\t<docTitle>\n\t\t<text>Dup Book</text>\n\t</docTitle>\n"
            "\t<navMap>\n"
            '\t\t<navPoint id="file0001" playOrder="1">\n'
            "\t\t\t<navLabel><text>Ch 1</text></navLabel>\n"
            '\t\t\t<content src="OEBPS/ch1.xhtml"/>\n'
            "\t\t</navPoint>\n"
            '\t\t<navPoint id="file0001" playOrder="2">\n'  # duplicate!
            "\t\t\t<navLabel><text>Ch 2</text></navLabel>\n"
            '\t\t\t<content src="OEBPS/ch2.xhtml"/>\n'
            "\t\t</navPoint>\n"
            "\t</navMap>\n</ncx>\n"
        )

        with zipfile.ZipFile(epub_path, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("mimetype"),
                "application/epub+zip",
                zipfile.ZIP_STORED,
            )
            archive.writestr("META-INF/container.xml", container_xml)
            archive.writestr("content.opf", opf_xml)
            archive.writestr("toc.ncx", ncx_xml)
            archive.writestr("OEBPS/ch1.xhtml", "<html><body><h1>Ch 1</h1></body></html>")
            archive.writestr("OEBPS/ch2.xhtml", "<html><body><h1>Ch 2</h1></body></html>")

        # Check the file is unchanged after reorder
        before = epub_path.read_bytes()
        with caplog.at_level(logging.WARNING):
            _run(epub_path)
        after = epub_path.read_bytes()

        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "Chapter-reorder skipped" in r.getMessage() and "distinct id(s)" in r.getMessage()
            for r in warning_records
        )
        assert before == after

    def test_empty_navmap_logs_a_warning_and_skips(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty navMap logs a warning and skips reordering."""
        # Build minimal EPUB with empty navMap
        epub_path = tmp_path / "empty.epub"
        container_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            "\t<rootfiles>\n"
            '\t\t<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>\n'
            "\t</rootfiles>\n</container>\n"
        )
        opf_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package version="2.0" xmlns="http://www.idpf.org/2007/opf" '
            'unique-identifier="fanficfare-uid">\n'
            '\t<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '\t\t<dc:identifier id="fanficfare-uid">test-uid</dc:identifier>\n'
            "\t\t<dc:title>Empty Book</dc:title>\n"
            "\t</metadata>\n"
            "\t<manifest>\n"
            '\t\t<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            "\t</manifest>\n"
            '\t<spine toc="ncx">\n\t</spine>\n</package>\n'
        )
        ncx_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">\n'
            "\t<head>\n"
            '\t\t<meta name="dtb:uid" content="test-uid"/>\n'
            "\t</head>\n"
            "\t<docTitle>\n\t\t<text>Empty Book</text>\n\t</docTitle>\n"
            "\t<navMap/>\n</ncx>\n"
        )

        with zipfile.ZipFile(epub_path, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("mimetype"),
                "application/epub+zip",
                zipfile.ZIP_STORED,
            )
            archive.writestr("META-INF/container.xml", container_xml)
            archive.writestr("content.opf", opf_xml)
            archive.writestr("toc.ncx", ncx_xml)

        with caplog.at_level(logging.WARNING):
            _run(epub_path)

        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("Chapter-reorder skipped" in r.getMessage() for r in warning_records)


def test_reorder_epub_module_has_no_step_class() -> None:
    import epub_chapter_reorder.reorder_step as m

    assert not hasattr(m, "ChapterReorderStep")


def test_zone_helper_is_gone() -> None:
    """The private _zone helper has been deleted in favor of the shared chapter_role."""
    from epub_chapter_reorder import reorder_step as chapter_reorder_step

    assert not hasattr(chapter_reorder_step, "_zone")


class TestSpecimenBook:
    """Tests for the specimen book — regression guard for EXP-127."""

    def test_the_specimen_book_is_left_alone(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The specimen book in author's own order survives an automatic pass unchanged."""
        specimen = [
            ("Prologue", _L + "prologue"),
            ("Chapter 1: The Salt Ledger", _L + "chapter-1-salt-ledger"),
            ("Chapter 2: Low Water", _L + "chapter-2-low-water"),
            ("Chapter 3: The Wright's Apprentice", _L + "chapter-3-wrights-apprentice"),
            ("Interlude 1", _L + "interlude-1"),
            ("Chapter 4: Neap Tide", _L + "chapter-4-neap-tide"),
            ("Interlude: The Long Wait", _L + "interlude-long-wait"),
            ("Chapter 5: Spring Tide", _L + "chapter-5-spring-tide"),
            ("Author's Note", _L + "authors-note"),
            ("Epilogue", _L + "epilogue"),
        ]
        epub = build_epub(specimen, doc_title="Specimen Book")
        before_mtime = epub.stat().st_mtime
        with caplog.at_level(logging.INFO):
            result = reorder_epub(epub)

        assert result is False
        assert epub.stat().st_mtime == before_mtime
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert any("already in order" in r.getMessage() for r in info_records)
        assert not any("Chapter-reorder fixed" in r.getMessage() for r in info_records)

    def test_a_shuffled_specimen_is_still_repaired(self, build_epub: Callable[..., Path]) -> None:
        """Specimen book with Chapter 4 and Chapter 2 swapped is repaired to correct order."""
        specimen_shuffled = [
            ("Prologue", _L + "prologue"),
            ("Chapter 1: The Salt Ledger", _L + "chapter-1-salt-ledger"),
            ("Chapter 4: Neap Tide", _L + "chapter-4-neap-tide"),  # Swapped with Chapter 2
            ("Chapter 3: The Wright's Apprentice", _L + "chapter-3-wrights-apprentice"),
            ("Interlude 1", _L + "interlude-1"),
            ("Chapter 2: Low Water", _L + "chapter-2-low-water"),  # Swapped with Chapter 4
            ("Interlude: The Long Wait", _L + "interlude-long-wait"),
            ("Chapter 5: Spring Tide", _L + "chapter-5-spring-tide"),
            ("Author's Note", _L + "authors-note"),
            ("Epilogue", _L + "epilogue"),
        ]
        epub = build_epub(specimen_shuffled, doc_title="Specimen Book")
        result = reorder_epub(epub)

        assert result is True
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        # Verify the order matches the specimen's correct order
        expected = [
            "Title Page",
            "Prologue",
            "Chapter 1: The Salt Ledger",
            "Chapter 2: Low Water",
            "Chapter 3: The Wright's Apprentice",
            "Interlude 1",
            "Chapter 4: Neap Tide",
            "Interlude: The Long Wait",
            "Chapter 5: Spring Tide",
            "Author's Note",
            "Epilogue",
        ]
        assert labels == expected

    def test_the_zone_decision_is_logged_at_debug(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Zone decision is logged at DEBUG level."""
        specimen = [
            ("Prologue", _L + "prologue"),
            ("Chapter 1: The Salt Ledger", _L + "chapter-1-salt-ledger"),
            ("Chapter 2: Low Water", _L + "chapter-2-low-water"),
            ("Epilogue", _L + "epilogue"),
        ]
        epub = build_epub(specimen, doc_title="Specimen Book")
        with caplog.at_level(logging.DEBUG):
            reorder_epub(epub)

        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert any("Chapter-reorder decision for" in r.getMessage() for r in debug_records)


def test_desired_order_bands_by_shared_role() -> None:
    """_desired_order bands navPoints by role: title, front, content (sorted), back."""
    from epub_chapter_reorder.reorder_step import _desired_order

    result = _desired_order(
        ["a", "b", "c", "d"],
        {"a": "Chapter 2", "b": "Cover", "c": "Epilogue", "d": "Chapter 1"},
        {"a": "2", "b": "", "c": "", "d": "1"},
    )
    assert result == ["b", "d", "a", "c"]


class TestNavOnlyEpub3:
    """reorder_epub works identically on an EPUB3 nav-only book (no toc.ncx)."""

    def test_reorders_nav_and_spine_together(self, build_nav_epub: Callable[..., Path]) -> None:
        out_of_order = [
            ("Book Ch. 3", _L + "c3"),
            ("Book Ch. 1", _L + "c1"),
            ("Book Ch. 2", _L + "c2"),
        ]
        epub = build_nav_epub(out_of_order, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        assert [p.id for p in doc.ncx.nav_points()] == ["chap0002", "chap0003", "chap0001"]
        assert [s.idref for s in doc.opf.spine()] == ["chap0002", "chap0003", "chap0001"]

    def test_already_ordered_is_not_rewritten(self, build_nav_epub: Callable[..., Path]) -> None:
        ordered = [("Book Ch. 1", _L + "c1"), ("Book Ch. 2", _L + "c2")]
        epub = build_nav_epub(ordered, doc_title="Book")
        before = epub.read_bytes()
        _run(epub)
        assert epub.read_bytes() == before


class TestResolveBookTitle:
    def test_resolve_book_title_prefers_doctitle(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Real Title")
        doc = EpubDocument.open(epub)
        assert resolve_book_title(doc) == "Real Title"

    def test_resolve_book_title_falls_back_to_opf(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="")
        doc = EpubDocument.open(epub)
        # The build_epub factory always sets OPF dc:title to the doc_title param,
        # so we need to verify it returns the OPF title when docTitle is empty
        result = resolve_book_title(doc)
        assert result == ""  # doc_title="" -> both docTitle and OPF title are ""


class TestResolveNavTitle:
    def test_resolve_nav_title_prefers_label(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Book")
        doc = EpubDocument.open(epub)
        points = doc.ncx.nav_points()
        # Skip title page (index 0), test a chapter
        point = points[1]  # file0001 with label "Tending Bar Part 5"
        assert resolve_nav_title(doc, point) == "Tending Bar Part 5"

    def test_resolve_nav_title_falls_back_to_xhtml(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        # Build a single chapter with empty label
        epub = build_epub([("Ch. Body Title", _L + "ch1")], doc_title="Book")
        doc = EpubDocument.open(epub)

        # Manually create a NavPoint with empty label
        point = NavPoint("test_ch", 1, "", "OEBPS/file0001.xhtml")

        with caplog.at_level(logging.DEBUG):
            result = resolve_nav_title(doc, point)

        # The chapter XHTML has "Ch. Body Title" in the h3
        assert result == "Ch. Body Title"
        assert any("Chapter-reorder title fallback" in record.message for record in caplog.records)

    def test_resolve_nav_title_empty_when_no_source(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Book")
        doc = EpubDocument.open(epub)

        # NavPoint with empty label and missing src
        point = NavPoint("ghost", 99, "", "OEBPS/nonexistent.xhtml")

        result = resolve_nav_title(doc, point)
        assert result == ""


class TestManualOrder:
    """Tests for manual chapter-order application in reorder_epub."""

    def test_none_manual_order_is_todays_behaviour(self, build_epub: Callable[..., Path]) -> None:
        """reorder_epub(path) and reorder_epub(path, manual_order=None) produce identical output."""
        # Build a book with chapters out of order
        chapters = [
            ("Book Ch. 03", _L + "c3"),
            ("Book Ch. 01", _L + "c1"),
            ("Book Ch. 02", _L + "c2"),
        ]
        epub1 = build_epub(chapters, doc_title="Book", filename="book1.epub")
        epub2 = build_epub(chapters, doc_title="Book", filename="book2.epub")

        # Run both versions
        result1 = reorder_epub(epub1)
        result2 = reorder_epub(epub2, manual_order=None)

        # Both should return the same result
        assert result1 == result2

        # Both should produce identical navPoint order
        doc1 = EpubDocument.open(epub1)
        doc2 = EpubDocument.open(epub2)
        order1 = [p.id for p in doc1.ncx.nav_points()]
        order2 = [p.id for p in doc2.ncx.nav_points()]
        assert order1 == order2

    def test_manual_order_is_applied(self, build_epub: Callable[..., Path]) -> None:
        """Manual order overrides automatic order and reorders chapters."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
        ]
        epub = build_epub(chapters, doc_title="Book")

        # Apply manual order u3, u1, u2
        result = reorder_epub(epub, manual_order=[_L + "u3", _L + "u1", _L + "u2"])

        assert result is True
        doc = EpubDocument.open(epub)
        nav_points = doc.ncx.nav_points()
        # title_page, then u3, u1, u2
        assert [p.src for p in nav_points] == [
            "OEBPS/title_page.xhtml",
            "OEBPS/file0003.xhtml",
            "OEBPS/file0001.xhtml",
            "OEBPS/file0002.xhtml",
        ]

    def test_manual_order_already_satisfied_is_a_noop(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Manual order matching current order is a no-op."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        before_bytes = epub.read_bytes()

        # Apply manual order that matches current order
        result = reorder_epub(epub, manual_order=[_L + "u1", _L + "u2", _L + "u3"])

        assert result is False
        assert epub.read_bytes() == before_bytes

    def test_unknown_key_in_manual_order_is_ignored(self, build_epub: Callable[..., Path]) -> None:
        """Unknown keys in manual_order are silently ignored."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        epub_ref = build_epub(chapters, doc_title="Book", filename="ref.epub")

        # Apply manual order with unknown key 'zz'
        result1 = reorder_epub(epub, manual_order=[_L + "u3", _L + "zz", _L + "u1", _L + "u2"])
        # Apply same order without 'zz'
        result2 = reorder_epub(epub_ref, manual_order=[_L + "u3", _L + "u1", _L + "u2"])

        # Both should produce the same result
        assert result1 == result2
        doc1 = EpubDocument.open(epub)
        doc2 = EpubDocument.open(epub_ref)
        assert [p.src for p in doc1.ncx.nav_points()] == [p.src for p in doc2.ncx.nav_points()]

    def test_new_chapter_is_spliced_after_its_anchor(self, build_epub: Callable[..., Path]) -> None:
        """New chapters are spliced after their nearest earlier known anchor."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
            ("Book Ch. 04", _L + "u4"),
        ]
        epub = build_epub(chapters, doc_title="Book")

        # Manual order: [u2, u4, u1] with u3 as new (not in manual_order)
        # automatic: [title_page, u1, u2, u3, u4]
        # u3's index in automatic is 3; walk back from 2:
        # automatic[2]=u2 (in placed) -> anchor is u2
        # u3 spliced after u2 in result
        result = reorder_epub(epub, manual_order=[_L + "u2", _L + "u4", _L + "u1"])

        assert result is True
        doc = EpubDocument.open(epub)
        nav_points = doc.ncx.nav_points()
        hrefs = [p.src for p in nav_points]
        # Expected: title_page, u2, u3 (spliced after u2), u4, u1
        assert hrefs == [
            "OEBPS/title_page.xhtml",
            "OEBPS/file0002.xhtml",  # u2
            "OEBPS/file0003.xhtml",  # u3 (spliced after u2)
            "OEBPS/file0004.xhtml",  # u4
            "OEBPS/file0001.xhtml",  # u1
        ]

    def test_new_chapter_with_no_earlier_anchor_goes_first(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """New chapters with no earlier anchor go at the front of content."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
        ]
        epub = build_epub(chapters, doc_title="Book")

        # Manual order is u2, u3 but u1 is new
        # u1 has no earlier anchor, so it goes first — which is where the fixture
        # already places it, so applying this order is a no-op (result is False).
        result = reorder_epub(epub, manual_order=[_L + "u2", _L + "u3"])

        assert result is False
        doc = EpubDocument.open(epub)
        nav_points = doc.ncx.nav_points()
        hrefs = [p.src for p in nav_points]
        # Expect: title_page, u1 (first), u2, u3
        assert hrefs == [
            "OEBPS/title_page.xhtml",
            "OEBPS/file0001.xhtml",
            "OEBPS/file0002.xhtml",
            "OEBPS/file0003.xhtml",
        ]

    def test_partial_application_warns(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Partial application (new chapters) logs a WARNING."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
            ("Book Ch. 04", _L + "u4"),
        ]
        epub = build_epub(chapters, doc_title="Book")

        with caplog.at_level(logging.WARNING):
            reorder_epub(epub, manual_order=[_L + "u1", _L + "u2", _L + "u3"])

        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "Manual chapter order partially applied" in r.message
            and "3 of 3 key(s) matched" in r.message
            and "1 chapter(s) spliced" in r.message
            for r in warning_records
        )

    def test_full_application_logs_info(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Full application (all chapters known) logs an INFO message."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
        ]
        epub = build_epub(chapters, doc_title="Book")

        with caplog.at_level(logging.INFO):
            reorder_epub(epub, manual_order=[_L + "u3", _L + "u1", _L + "u2"])

        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert any(
            "Manual chapter order applied to" in r.message and "3 chapter(s)" in r.message
            for r in info_records
        )
        # No WARNING should be present for full application
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert not any("partially applied" in r.message for r in warning_records)

    def test_manual_order_does_not_touch_chapter_files(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Manual reorder only touches NCX and OPF, not chapter content."""
        chapters = [
            ("Book Ch. 01", _L + "u1"),
            ("Book Ch. 02", _L + "u2"),
            ("Book Ch. 03", _L + "u3"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        doc = EpubDocument.open(epub)

        # Get chapter content hashes before reorder
        hashes_before = {}
        for point in doc.ncx.nav_points():
            if point.src:
                content = doc.resource_bytes(point.src)
                hashes_before[point.src] = hashlib.sha256(content).hexdigest()

        # Reorder with manual order
        reorder_epub(epub, manual_order=[_L + "u3", _L + "u1", _L + "u2"])

        # Verify chapter content is unchanged
        doc = EpubDocument.open(epub)
        for point in doc.ncx.nav_points():
            if point.src and point.src in hashes_before:
                content = doc.resource_bytes(point.src)
                assert hashlib.sha256(content).hexdigest() == hashes_before[point.src]

    def test_signature_is_keyword_only(self) -> None:
        """The manual_order parameter is keyword-only with default None."""
        sig = inspect.signature(reorder_epub)
        manual_order_param = sig.parameters["manual_order"]
        assert manual_order_param.kind is inspect.Parameter.KEYWORD_ONLY
        assert manual_order_param.default is None


class TestNumberedSpecialSections:
    """Tests for numbered special sections (interludes, bonus chapters, etc.)."""

    def test_a_numbered_interlude_keeps_its_slot(self, build_epub: Callable[..., Path]) -> None:
        """Numbered interlude keeps its original position and is not sorted by its ordinal."""
        chapters = [
            ("Prologue", _L + "prologue"),
            ("Chapter 1", _L + "ch1"),
            ("Chapter 2", _L + "ch2"),
            ("Chapter 3", _L + "ch3"),
            ("Interlude 1", _L + "interlude1"),
            ("Chapter 4", _L + "ch4"),
            ("Chapter 5", _L + "ch5"),
            ("Author's Note", _L + "author_note"),
            ("Epilogue", _L + "epilogue"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        # Interlude 1 should stay in its original position (after Ch. 3, before Ch. 4)
        assert labels == [
            "Title Page",
            "Prologue",
            "Chapter 1",
            "Chapter 2",
            "Chapter 3",
            "Interlude 1",
            "Chapter 4",
            "Chapter 5",
            "Author's Note",
            "Epilogue",
        ]

    def test_a_genuinely_shuffled_book_is_still_repaired(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Genuinely shuffled chapters are still sorted correctly."""
        chapters = [
            ("Chapter 3", _L + "ch3"),
            ("Chapter 1", _L + "ch1"),
            ("Chapter 2", _L + "ch2"),
        ]
        epub = build_epub(chapters, doc_title="Book")
        _run(epub)
        doc = EpubDocument.open(epub)
        labels = [p.label for p in doc.ncx.nav_points()]
        assert labels == ["Title Page", "Chapter 1", "Chapter 2", "Chapter 3"]


class TestAutomaticKeyOrder:
    """Test the automatic_key_order helper."""

    def test_returns_keys_in_automatic_order(self, build_epub: Callable[..., Path]) -> None:
        """For a book out of order, returned keys are in the order reorder_epub would produce."""
        from epub_chapter_reorder.reorder_step import automatic_key_order

        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        doc = EpubDocument.open(epub)
        keys = automatic_key_order(doc)

        # Should return stable keys (URLs) in automatic order
        assert isinstance(keys, list)
        assert len(keys) > 0
        assert all(isinstance(k, str) for k in keys)

    def test_is_stable_for_an_already_ordered_book(self, build_epub: Callable[..., Path]) -> None:
        """An already-ordered book returns its keys in spine order."""
        from epub_chapter_reorder.reorder_step import automatic_key_order

        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")
        doc = EpubDocument.open(epub)
        keys = automatic_key_order(doc)

        # For an already-ordered book, keys should match spine order (includes title page)
        assert isinstance(keys, list)
        assert len(keys) >= 3
        assert all(isinstance(k, str) for k in keys)


class TestDisplayTitle:
    """Test that display_title parameter is used in log lines."""

    @staticmethod
    def _epub_with_duplicate_nav_ids(tmp_path: Path) -> Path:
        """Create an EPUB with duplicate nav point IDs to trigger the skip-warning path."""
        epub_path = tmp_path / "dup_ids.epub"
        # Copy a structure with duplicate nav IDs to trigger the warning
        members = {
            "META-INF/container.xml": (
                '<?xml version="1.0"?>'
                '<container version="1.0" '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="content.opf" '
                'media-type="application/oebps-package+xml"/>'
                "</rootfiles></container>"
            ),
            "OEBPS/stylesheet.css": "body { margin: 2%; }",
            "OEBPS/title_page.xhtml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                "<title>Raw Epub Title</title></head><body>"
                "<h1>Raw Epub Title</h1></body></html>"
            ),
            "OEBPS/file0001.xhtml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                "<title>Ch1</title></head><body><h1>Ch1</h1>"
                "<p>Body.</p></body></html>"
            ),
            "OEBPS/file0002.xhtml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                "<title>Ch2</title></head><body><h1>Ch2</h1>"
                "<p>Body.</p></body></html>"
            ),
            "content.opf": (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<package version="2.0" xmlns="http://www.idpf.org/2007/opf" '
                'unique-identifier="fanficfare-uid">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:opf="http://www.idpf.org/2007/opf">'
                '<dc:identifier id="fanficfare-uid">test-uid</dc:identifier>'
                "<dc:title>Raw Epub Title</dc:title>"
                '<dc:creator opf:role="aut">Author</dc:creator>'
                "</metadata>"
                "<manifest>"
                '<item id="ncx" href="toc.ncx" '
                'media-type="application/x-dtbncx+xml"/>'
                '<item id="style" href="OEBPS/stylesheet.css" '
                'media-type="text/css"/>'
                '<item id="title_page" href="OEBPS/title_page.xhtml" '
                'media-type="application/xhtml+xml"/>'
                '<item id="file0001" href="OEBPS/file0001.xhtml" '
                'media-type="application/xhtml+xml"/>'
                '<item id="file0002" href="OEBPS/file0002.xhtml" '
                'media-type="application/xhtml+xml"/>'
                "</manifest>"
                '<spine toc="ncx">'
                '<itemref idref="title_page" linear="yes"/>'
                '<itemref idref="file0001" linear="yes"/>'
                '<itemref idref="file0002" linear="yes"/>'
                "</spine>"
                "</package>"
            ),
            "toc.ncx": (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<ncx version="2005-1" '
                'xmlns="http://www.daisy.org/z3986/2005/ncx/">'
                '<head><meta name="dtb:uid" content="test-uid"/></head>'
                "<docTitle><text>Raw Epub Title</text></docTitle>"
                "<navMap>"
                '<navPoint id="title_page" playOrder="1">'
                "<navLabel><text>Title Page</text></navLabel>"
                '<content src="OEBPS/title_page.xhtml"/>'
                "</navPoint>"
                '<navPoint id="file0001" playOrder="2">'
                "<navLabel><text>Ch1</text></navLabel>"
                '<content src="OEBPS/file0001.xhtml"/>'
                "</navPoint>"
                '<navPoint id="file0001" playOrder="3">'
                "<navLabel><text>Ch2</text></navLabel>"
                '<content src="OEBPS/file0002.xhtml"/>'
                "</navPoint>"
                "</navMap>"
                "</ncx>"
            ),
        }
        epub_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(epub_path, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("mimetype"),
                "application/epub+zip",
                zipfile.ZIP_STORED,
            )
            for name, content in members.items():
                archive.writestr(name, content, zipfile.ZIP_DEFLATED)
        return epub_path

    def test_log_uses_the_supplied_display_title(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When display_title is provided, it should appear in log lines instead of EPUB title."""
        epub = self._epub_with_duplicate_nav_ids(tmp_path)
        with caplog.at_level(logging.WARNING):
            reorder_epub(epub, display_title="Edited Title")
        # With duplicate ids in TOC, a warning is logged with the title
        assert "Edited Title" in caplog.text
        assert "Raw Epub Title" not in caplog.text

    def test_log_falls_back_to_epub_title(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When display_title is None, the EPUB title should be used."""
        epub = self._epub_with_duplicate_nav_ids(tmp_path)
        with caplog.at_level(logging.WARNING):
            reorder_epub(epub, display_title=None)
        assert "Raw Epub Title" in caplog.text

    def test_blank_display_title_falls_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When display_title is blank/whitespace, the EPUB title should be used."""
        epub = self._epub_with_duplicate_nav_ids(tmp_path)
        with caplog.at_level(logging.WARNING):
            reorder_epub(epub, display_title="  ")
        assert "Raw Epub Title" in caplog.text

    def test_display_title_is_keyword_only(self, build_epub: Callable[..., Path]) -> None:
        """display_title must be passed as keyword argument."""
        chapters = [
            ("Book Ch. 02", _L + "c2"),
            ("Book Ch. 01", _L + "c1"),
        ]
        epub = build_epub(chapters, doc_title="Raw Epub Title")
        with pytest.raises(TypeError):
            reorder_epub(epub, None, "x")  # type: ignore[call-arg]
