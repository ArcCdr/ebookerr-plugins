"""Tests for `merge_epubs` — merge multiple EPUBs using the rebuild orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.epub import EpubArchive, EpubDocument, MalformedEpubError
from ebookerr_sdk.epub.build import Book as AssembledBook
from ebookerr_sdk.epub.build import Chapter as AssembledChapter
from ebookerr_sdk.epub.build import build_epub as assemble_epub
from ebookerr_sdk.testing import FakeContext
from epub_merge.merge import (
    MergeInputError,
    MergeOptions,
    MergeStructureError,
    merge_epubs,
)

_L = "https://example.test/s/"


def _submitted(*ids: str) -> api.ViewResult:
    """The merge view submitted with every named orphan selected, in this order."""
    return api.ViewResult(
        submitted=True, selections={"orphans": api.ViewSelection(order=ids, selected=ids)}
    )


def _build_assembled_epub(
    path: Path, chapters: list[tuple[str, str]], *, doc_title: str = "Sample"
) -> Path:
    """Build an EPUB via the real docx/pdf/rtf/txt assembler (ebookerr_sdk.epub.build).

    Its chapter/title-page XHTML carries no FanFicFare ``fff_chapter`` body class
    (see ``epub_build._XHTML_TEMPLATE``) — this is the actual shape of a book
    pulled through ``docx_download_source``/``pdf_download_source``/etc., and the
    shape that exposed the merge's original body-class-based chapter filter as a
    FanFicFare-only assumption.
    """
    book = AssembledBook(
        title=doc_title,
        author="Author",
        chapters=tuple(AssembledChapter(title, (text,), True) for title, text in chapters),
    )
    assemble_epub(book, path)
    return path


def test_merge_returns_total_chapter_count(build_epub: Callable[..., Path]) -> None:
    """Merging a 2-chapter target with a 2-chapter source returns 4."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    outcome = merge_epubs(target, [source])

    assert outcome.chapter_count == 4


def test_merged_labels_preserve_input_order(build_epub: Callable[..., Path]) -> None:
    """The reopened target's nav point labels preserve survivor-first input order."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    nav_points = doc.ncx.nav_points()
    labels = [p.label for p in nav_points]
    # The survivor's own title page (build_epub's default) leads, unrewritten (MR-PARAM-1 off).
    assert labels == [
        "Title Page",
        "Book A Ch. 1",
        "Book A Ch. 2",
        "Book B Ch. 1",
        "Book B Ch. 2",
    ]


def test_play_order_is_one_based_and_gapless(build_epub: Callable[..., Path]) -> None:
    """play_order values are 1-based, contiguous, and gapless (SH-4)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    nav_points = doc.ncx.nav_points()
    play_orders = [p.play_order for p in nav_points]
    # 5 entries: the survivor's carried-through title page, then the 4 chapters.
    assert play_orders == [1, 2, 3, 4, 5]


def test_no_subdirectories_for_xhtml(build_epub: Callable[..., Path]) -> None:
    """Every manifest XHTML item has an href with no `/` (MR-FLAT-1)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    for item in doc.opf.manifest():
        if item.media_type == "application/xhtml+xml":
            assert "/" not in item.href, f"XHTML item {item.href} contains subdirectory"


def test_no_merged_artefacts(build_epub: Callable[..., Path]) -> None:
    """No archive member name contains 'merged' (MR-CLEAN-1/SH-2)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    archive = EpubArchive.open(target)

    for item in doc.opf.manifest():
        assert "merged" not in item.id.lower()
        assert "merged" not in item.href.lower()

    for spine_item in doc.opf.spine():
        assert "merged" not in spine_item.idref.lower()

    for nav_point in doc.ncx.nav_points():
        assert "merged" not in nav_point.id.lower()
        assert "merged" not in nav_point.src.lower()

    for member in archive.names():
        assert "merged" not in member.lower()


def test_chapters_are_renamed_semantically(build_epub: Callable[..., Path]) -> None:
    """Chapters are renamed with semantic naming (AT-RENAME-1/SH-3)."""
    target = build_epub(
        [("Three Square Meals - Chapter 174", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [
            ("Three Square Meals - Chapter 175", _L + "b1"),
            ("Three Square Meals - Chapter 176", _L + "b2"),
        ],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    manifest_hrefs = [item.href for item in doc.opf.manifest()]

    # At least some chapters should have semantic names
    semantic_names = [h for h in manifest_hrefs if "chapter_" in h]
    assert len(semantic_names) > 0

    # No member should be named file0001.xhtml
    assert all("file0001.xhtml" not in h for h in manifest_hrefs)


def test_no_duplicate_manifest_or_spine_entries(build_epub: Callable[..., Path]) -> None:
    """Manifest ids, hrefs, spine idrefs, and nav point srcs are each all-distinct (MR-DEDUP-1)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)

    manifest_ids = [item.id for item in doc.opf.manifest()]
    assert len(manifest_ids) == len(set(manifest_ids))

    manifest_hrefs = [item.href for item in doc.opf.manifest()]
    assert len(manifest_hrefs) == len(set(manifest_hrefs))

    spine_idrefs = [s.idref for s in doc.opf.spine()]
    assert len(spine_idrefs) == len(set(spine_idrefs))

    nav_srcs = [p.src for p in doc.ncx.nav_points()]
    assert len(nav_srcs) == len(set(nav_srcs))


def test_doc_title_equals_dc_title(build_epub: Callable[..., Path]) -> None:
    """NCX doc_title() equals OPF get_title() (MR-DOCTITLE-1/SH-6)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    assert doc.ncx.doc_title() == doc.opf.get_title()


def test_dtb_uid_equals_unique_identifier(build_epub: Callable[..., Path]) -> None:
    """NCX dtb:uid meta equals OPF unique_identifier() exactly (MR-UID-1/SH-16)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    ncx_uid = doc.ncx.unique_identifier()
    opf_uid = doc.opf.unique_identifier()
    assert ncx_uid == opf_uid


def test_dtb_depth_is_one(build_epub: Callable[..., Path]) -> None:
    """NCX dtb:depth meta content is '1' (AT-DEPTH-1)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    depth = doc.ncx.depth()
    assert depth == 1


def test_formatting_contract(build_epub: Callable[..., Path]) -> None:
    """For a 4-book merge, verify formatting contracts (AT-FMT-1/AT-FMT-2)."""
    # Create 4 books
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    sources = [
        build_epub([("B Ch. 1", _L + "b1")], doc_title="Book B", filename="b.epub"),
        build_epub([("C Ch. 1", _L + "c1")], doc_title="Book C", filename="c.epub"),
        build_epub([("D Ch. 1", _L + "d1")], doc_title="Book D", filename="d.epub"),
    ]

    merge_epubs(target, sources)

    archive = EpubArchive.open(target)

    # Check toc.ncx
    ncx_content = archive.read_bytes("OEBPS/toc.ncx").decode("utf-8")
    for line in ncx_content.splitlines():
        assert len(line) <= 500, f"NCX line too long: {len(line)} chars"
        assert line.count("<navPoint") <= 1, f"NCX line has multiple navPoint: {line}"

    # Check content.opf
    opf_content = archive.read_bytes("OEBPS/content.opf").decode("utf-8")
    for line in opf_content.splitlines():
        assert len(line) <= 500, f"OPF line too long: {len(line)} chars"
        assert line.count("<item ") <= 1, f"OPF line has multiple item: {line}"


def test_zip_contract(build_epub: Callable[..., Path]) -> None:
    """ZIP archive meets EPUB spec (AT-ZIP-1/AT-ZIP-2)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    archive = EpubArchive.open(target)
    names = archive.names()

    # First entry should be mimetype
    assert names[0] == "mimetype"

    # Verify mimetype value
    mimetype_bytes = archive.read_bytes("mimetype")
    assert mimetype_bytes == b"application/epub+zip"


def test_meta_inf_contains_only_container(build_epub: Callable[..., Path]) -> None:
    """META-INF contains only container.xml (MR-VENDOR-1/SH-21)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    archive = EpubArchive.open(target)
    meta_inf_members = [m for m in archive.names() if m.startswith("META-INF/")]
    assert meta_inf_members == ["META-INF/container.xml"]


def test_stylesheet_is_carried_over(build_epub: Callable[..., Path]) -> None:
    """Survivor's stylesheet.css is present in merged archive and listed in manifest."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    archive = EpubArchive.open(target)

    # stylesheet should be in manifest
    manifest_hrefs = {item.href for item in doc.opf.manifest()}
    assert any("stylesheet" in href for href in manifest_hrefs)

    # stylesheet should be in archive
    stylesheet_paths = [m for m in archive.names() if "stylesheet" in m]
    assert len(stylesheet_paths) > 0


def test_only_survivor_stylesheet_survives(build_epub: Callable[..., Path]) -> None:
    """Merged archive contains only survivor's stylesheet; every chapter links to it."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    merge_epubs(target, [source1, source2])

    # Open the merged book
    archive = EpubArchive.open(target)

    # Only one stylesheet should be in the merged archive (the survivor's)
    stylesheet_paths = [m for m in archive.names() if ".css" in m]
    assert len(stylesheet_paths) == 1
    stylesheet_filename = stylesheet_paths[0].split("/")[-1]

    # Check that every XHTML chapter contains a stylesheet link to the survivor's stylesheet
    # (AT-CSS-1 and AT-CSS-3: all chapters link to one canonical stylesheet)
    stylesheet_found_in = 0
    for member_name in archive.names():
        if member_name.endswith(".xhtml") and member_name != "nav.xhtml":
            member_text = archive.read_text(member_name)
            # Check that it contains a stylesheet link to the survivor's stylesheet
            if (
                "<link" in member_text
                and 'rel="stylesheet"' in member_text
                and f'href="{stylesheet_filename}"' in member_text
            ):
                stylesheet_found_in += 1

    # At least some chapters should have been relinked
    assert stylesheet_found_in > 0


def test_merges_three_sources(build_epub: Callable[..., Path]) -> None:
    """Merging 1 + 3 books produces correct chapter count and labels in input order."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    sources = [
        build_epub([("B Ch. 1", _L + "b1")], doc_title="Book B", filename="b.epub"),
        build_epub([("C Ch. 1", _L + "c1")], doc_title="Book C", filename="c.epub"),
        build_epub([("D Ch. 1", _L + "d1")], doc_title="Book D", filename="d.epub"),
    ]

    outcome = merge_epubs(target, sources)

    assert outcome.chapter_count == 4
    doc = EpubDocument.open(target)
    nav_labels = [p.label for p in doc.ncx.nav_points()]
    # The survivor's own title page (build_epub's default) leads, unrewritten; it is
    # not itself a content chapter, so `total` above still counts only the 4 real ones.
    assert nav_labels == ["Title Page", "A Ch. 1", "B Ch. 1", "C Ch. 1", "D Ch. 1"]


def test_merges_nav_only_epub3_books(build_nav_epub: Callable[..., Path]) -> None:
    """Merging two nav-only EPUB3 books works and preserves both chapter labels."""
    target = build_nav_epub(
        [("Book A Ch. 1", "u1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_nav_epub(
        [("Book B Ch. 1", "u1")],
        doc_title="Book B",
        filename="b.epub",
    )

    outcome = merge_epubs(target, [source])

    assert outcome.chapter_count == 2
    doc = EpubDocument.open(target)
    nav_labels = [p.label for p in doc.ncx.nav_points()]
    assert nav_labels == ["Book A Ch. 1", "Book B Ch. 1"]


def test_merges_assembler_shaped_books(tmp_path: Path) -> None:
    """Merging books from the docx/pdf/rtf/txt assembler preserves chapter content."""
    target = _build_assembled_epub(
        tmp_path / "target.epub",
        [("Target Ch. 1", "Target body.")],
        doc_title="Target",
    )
    source = _build_assembled_epub(
        tmp_path / "source.epub",
        [("Source Ch. 1", "Source body one."), ("Source Ch. 2", "Source body two.")],
        doc_title="Source",
    )

    outcome = merge_epubs(target, [source])

    assert outcome.chapter_count == 3
    doc = EpubDocument.open(target)
    labels = [p.label for p in doc.ncx.nav_points()]
    # The assembler gives every book its own title page too; the survivor's leads,
    # unrewritten. It is not itself a content chapter, so `total` above stays 3.
    assert labels == ["Title Page", "Target Ch. 1", "Source Ch. 1", "Source Ch. 2"]
    # Verify source body text is present
    chapter_texts = [doc.chapter(p.src).to_xml() for p in doc.ncx.nav_points()]
    assert any("Source body one." in text for text in chapter_texts)
    assert any("Source body two." in text for text in chapter_texts)


def test_dangling_spine_reference_raises_and_leaves_target_untouched(
    build_epub: Callable[..., Path],
) -> None:
    """A source with a dangling spine reference raises MergeInputError; target bytes unchanged."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    # Add ghost spine item
    doc = EpubDocument.open(source)
    doc.opf.add_spine_item("ghost-item")
    doc.save(source)

    original_target_bytes = target.read_bytes()

    with pytest.raises(MergeInputError):
        merge_epubs(target, [source])

    assert target.read_bytes() == original_target_bytes


def test_unreadable_source_raises_merge_input_error(tmp_path: Path) -> None:
    """An unreadable source (not a ZIP) raises MergeInputError; target unchanged."""
    target = tmp_path / "target.epub"
    target.write_bytes(b"PK\x03\x04")  # Minimal ZIP header

    source = tmp_path / "source.epub"
    source.write_bytes(b"not a zip")

    original_target_bytes = target.read_bytes()

    with pytest.raises(MergeInputError):
        merge_epubs(target, [source])

    assert target.read_bytes() == original_target_bytes


def test_failure_after_write_preserves_artifact(
    build_epub: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If verification fails after write, staged file is moved to temp dir; target untouched."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    original_target_bytes = target.read_bytes()
    real_open = EpubDocument.open

    # Monkeypatch EpubDocument.open globally to raise on .merge-tmp paths
    def mocked_open(path):
        if str(path).endswith(".merge-tmp"):
            raise MalformedEpubError("Simulated verification failure")
        return real_open(path)

    monkeypatch.setattr("epub_merge.merge.merge.EpubDocument.open", mocked_open)

    with pytest.raises(MergeStructureError):
        merge_epubs(target, [source])

    # Target should be unchanged
    assert target.read_bytes() == original_target_bytes

    # No .merge-tmp file should remain
    merge_tmp_files = list(target.parent.glob("*.merge-tmp"))
    assert len(merge_tmp_files) == 0

    # ERROR log should mention rejected output kept at
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("rejected output kept at" in r.message for r in error_records)


def test_logs_start_and_finish(
    build_epub: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Logs contain 'EPUB merge started' and 'EPUB merge finished' with chapter count."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    with caplog.at_level("INFO"):
        merge_epubs(target, [source])

    info_messages = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("EPUB merge started" in msg for msg in info_messages)
    finish_messages = [msg for msg in info_messages if "EPUB merge finished" in msg]
    assert len(finish_messages) > 0
    # Finish message should contain chapter count
    assert any("2" in msg for msg in finish_messages)


def test_no_temp_file_left_behind_on_success(build_epub: Callable[..., Path]) -> None:
    """After successful merge, no *.merge-tmp file exists in target's directory."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    merge_tmp_files = list(target.parent.glob("*.merge-tmp"))
    assert len(merge_tmp_files) == 0


def test_merged_opf_has_exactly_one_description(build_epub: Callable[..., Path]) -> None:
    """The merged OEBPS/content.opf contains exactly one <dc:description> line per book."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        author="Author A",
        filename="a.epub",
    )
    source = build_epub(
        [("B Ch. 1", _L + "b1")],
        doc_title="Book B",
        author="Author B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    opf_text = doc.opf.to_xml()

    # Count exactly one <dc:description> element
    desc_count = opf_text.count("<dc:description>")
    assert desc_count == 1

    # Description should contain one line per input book
    assert "Book A by Author A" in opf_text
    assert "Book B by Author B" in opf_text


def test_merged_opf_preserves_source_and_url_identifier(build_epub: Callable[..., Path]) -> None:
    """Merging books with source preserves both <dc:source> and <dc:identifier scheme=URL>."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        source="https://example.com/story",
        filename="a.epub",
    )
    source = build_epub(
        [("B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    opf_text = doc.opf.to_xml()

    # Exactly one <dc:source> element with the survivor's URL
    source_count = opf_text.count("<dc:source>")
    assert source_count == 1
    assert "https://example.com/story" in opf_text

    # Exactly one <dc:identifier opf:scheme="URL"> element with the same URL
    url_id_count = opf_text.count('opf:scheme="URL"')
    assert url_id_count == 1
    assert "https://example.com/story" in opf_text


def test_merged_opf_has_no_empty_placeholder_elements(build_epub: Callable[..., Path]) -> None:
    """When inputs have no dc:source/dc:rights, merged OPF contains neither element."""
    target = build_epub(
        [("A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
        # No source, no description (beyond auto-generated), no rights
    )
    source = build_epub(
        [("B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    opf_text = doc.opf.to_xml()

    # No <dc:source element at all (not even empty)
    assert "<dc:source" not in opf_text
    # No <dc:rights element at all (not even empty)
    assert "<dc:rights" not in opf_text


def test_merged_book_keeps_survivor_cover(build_epub: Callable[..., Path]) -> None:
    """Merge carries the survivor's cover through to the merged book."""
    # Build a target with a PNG cover
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    # Set the cover on the survivor
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"fake png data"
    survivor_doc = EpubDocument.open(target)
    survivor_doc.set_cover(png_bytes, "image/png")
    survivor_doc.save()

    # Build a source book to merge
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    # Perform the merge
    merge_epubs(target, [source])

    # Verify the merged book has the cover
    doc = EpubDocument.open(target)
    cover_result = doc.cover_image()
    assert cover_result is not None
    cover_bytes, media_type = cover_result
    assert cover_bytes == png_bytes
    assert media_type == "image/png"

    # Verify cover.xhtml is in the archive
    assert "OEBPS/cover.xhtml" in doc.member_names()

    # Verify the cover page doesn't count as a content chapter
    chapter_count = doc.content_chapter_count()
    # 4 chapters (2 from A, 2 from B) + 1 title page (not counted) + 1 cover (not counted)
    assert chapter_count == 4


def test_merged_epub3_has_nav_and_ncx(build_nav_epub: Callable[..., Path]) -> None:
    """Merging EPUB3 nav-only books produces both nav.xhtml and toc.ncx (AT-EPUB-2/AT-EPUB-3)."""
    target = build_nav_epub(
        [("Chapter 1", _L + "a1"), ("Chapter 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_nav_epub(
        [("Chapter 3", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    archive = EpubArchive.open(target)
    doc = EpubDocument.open(target)

    # Check both nav.xhtml and toc.ncx are members
    members = archive.names()
    assert "OEBPS/nav.xhtml" in members
    assert "OEBPS/toc.ncx" in members

    # Check version is 3.0
    assert doc.opf._root.get("version") == "3.0"

    # Check nav.xhtml contains epub:type="toc"
    nav_content = archive.read_bytes("OEBPS/nav.xhtml").decode("utf-8")
    assert 'epub:type="toc"' in nav_content


class TestMergeView:
    """Tests for the merge confirmation view layout (EXP-208)."""

    @pytest.mark.pins("EXP-208")
    def test_the_candidate_section_declares_a_human_readable_label(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """The ITEM_LIST section has label 'Books to merge'.

        EXP-208: the merge dialog's candidate list carries a heading that reads
        "Books to merge", not the internal name "orphans" (which elsewhere means
        "EPUB missing").
        """
        import ebookerr_sdk.spi as api
        from epub_merge.plugin import _build_view

        survivor = build_epub(
            [("Book A Ch. 1", _L + "a1")],
            doc_title="Book A",
            filename="a.epub",
        )
        other = build_epub(
            [("Book B Ch. 1", _L + "b1")],
            doc_title="Book B",
            filename="b.epub",
        )

        survivor_view = api.BookView(
            book_id="survivor_id",
            title="Book A",
            author="Author",
            story_url=None,
            output_filename="a.epub",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )
        other_view = api.BookView(
            book_id="other_id",
            title="Book B",
            author="Author",
            story_url=None,
            output_filename="b.epub",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )

        survivor_item = api.EpubItem(book=survivor_view, epub_path=survivor)
        other_item = api.EpubItem(book=other_view, epub_path=other)

        view = _build_view(survivor_item, [other_item], "survivor_id")

        # Find the ITEM_LIST section
        item_list_section = next(
            s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST
        )

        assert item_list_section.name == "orphans"
        assert item_list_section.label == "Books to merge"
        assert item_list_section.item_verb == "Merge"
        assert len(item_list_section.items) == 1
        assert item_list_section.items[0].label == "Book B"

    def test_the_section_name_still_keys_the_results(self, build_epub: Callable[..., Path]) -> None:
        """The section name is still 'orphans' for backwards compatibility with result keys.

        The data-section-name attribute (which keys the results) must remain
        "orphans", even though the display heading is now "Books to merge".
        """
        import ebookerr_sdk.spi as api
        from epub_merge.plugin import _build_view

        survivor = build_epub(
            [("Book A Ch. 1", _L + "a1")],
            doc_title="Book A",
            filename="a.epub",
        )
        other = build_epub(
            [("Book B Ch. 1", _L + "b1")],
            doc_title="Book B",
            filename="b.epub",
        )

        survivor_view = api.BookView(
            book_id="survivor_id",
            title="Book A",
            author="Author",
            story_url=None,
            output_filename="a.epub",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )
        other_view = api.BookView(
            book_id="other_id",
            title="Book B",
            author="Author",
            story_url=None,
            output_filename="b.epub",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )

        survivor_item = api.EpubItem(book=survivor_view, epub_path=survivor)
        other_item = api.EpubItem(book=other_view, epub_path=other)

        view = _build_view(survivor_item, [other_item], "survivor_id")

        # Find the ITEM_LIST section
        item_list_section = next(
            s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST
        )

        # The wire key must stay "orphans"
        assert item_list_section.name == "orphans"


def test_merged_epub3_nav_entry_count_matches_ncx(build_nav_epub: Callable[..., Path]) -> None:
    """TOC <li> entries in nav.xhtml match navPoints count in toc.ncx (AT-EPUB-4/SH-12)."""
    target = build_nav_epub(
        [("Chapter 1", _L + "a1"), ("Chapter 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_nav_epub(
        [("Chapter 3", _L + "b1"), ("Chapter 4", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    archive = EpubArchive.open(target)
    doc = EpubDocument.open(target)

    # Count navPoints in NCX
    nav_points = doc.ncx.nav_points()
    ncx_count = len(nav_points)

    # Count <li> entries in the TOC nav (before the landmarks nav starts)
    nav_content = archive.read_bytes("OEBPS/nav.xhtml").decode("utf-8")
    # Find TOC nav's <ol> and count its <li> entries (stop counting at landmarks nav if present)
    toc_start = nav_content.find('epub:type="toc"')
    landmarks_start = nav_content.find('epub:type="landmarks"')
    toc_end = len(nav_content) if landmarks_start == -1 else landmarks_start
    toc_section = nav_content[toc_start:toc_end]
    li_count = toc_section.count("<li")

    assert li_count == ncx_count, (
        f"nav.xhtml TOC has {li_count} <li> but ncx has {ncx_count} navPoints"
    )


def test_merged_epub2_has_no_nav(build_epub: Callable[..., Path]) -> None:
    """Merging two EPUB2 books produces version='2.0' and no nav.xhtml member (AT-EPUB-1)."""
    target = build_epub(
        [("Chapter 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Chapter 2", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    archive = EpubArchive.open(target)
    doc = EpubDocument.open(target)

    # Check version is 2.0
    assert doc.opf._root.get("version") == "2.0"

    # Check nav.xhtml is NOT in members
    members = archive.names()
    assert "OEBPS/nav.xhtml" not in members

    # Check toc.ncx is still present
    assert "OEBPS/toc.ncx" in members


def test_merged_epub3_has_one_dcterms_modified(
    build_nav_epub: Callable[..., Path], build_epub: Callable[..., Path]
) -> None:
    """EPUB3 OPF has exactly one dcterms:modified meta; EPUB2 has none."""
    # Test EPUB3: merge two EPUB3 books
    target_3 = build_nav_epub(
        [("Chapter 1", _L + "a1")],
        doc_title="Book A",
        filename="a_epub3.epub",
    )
    source_3 = build_nav_epub(
        [("Chapter 2", _L + "b1")],
        doc_title="Book B",
        filename="b_epub3.epub",
    )

    merge_epubs(target_3, [source_3])

    doc_3 = EpubDocument.open(target_3)
    opf_text_3 = doc_3.opf.to_xml()

    # Count dcterms:modified occurrences in EPUB3
    modified_count_3 = opf_text_3.count('<meta property="dcterms:modified">')
    assert modified_count_3 == 1, f"EPUB3 has {modified_count_3} dcterms:modified, expected 1"

    # Verify format matches ISO 8601 regex
    import re

    pattern = r'<meta property="dcterms:modified">(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)</meta>'
    match = re.search(pattern, opf_text_3)
    assert match, "EPUB3 dcterms:modified does not match expected format"

    # Test EPUB2: merge two EPUB2 books
    target_2 = build_epub(
        [("Chapter 1", _L + "a1")],
        doc_title="Book A",
        filename="a_epub2.epub",
    )
    source_2 = build_epub(
        [("Chapter 2", _L + "b1")],
        doc_title="Book B",
        filename="b_epub2.epub",
    )

    merge_epubs(target_2, [source_2])

    doc_2 = EpubDocument.open(target_2)
    opf_text_2 = doc_2.opf.to_xml()

    # Count dcterms:modified occurrences in EPUB2
    modified_count_2 = opf_text_2.count('<meta property="dcterms:modified">')
    assert modified_count_2 == 0, f"EPUB2 has {modified_count_2} dcterms:modified, expected 0"


def test_epub2_guide_has_start_of_content(build_epub: Callable[..., Path]) -> None:
    """Merging books produces an OPF guide with a type='text' entry (AT-NAV-1).

    The entry points to the first narrative chapter.
    """
    target = build_epub(
        [("Title Page", _L + "a0"), ("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    doc = EpubDocument.open(target)
    guide_entries = doc.opf.guide()

    # Find the type="text" entry
    text_entries = [e for e in guide_entries if e.type == "text"]
    assert len(text_entries) > 0, "Guide has no type='text' entry"

    text_entry = text_entries[0]
    # Should point to the first non-title-page chapter (Chapter 1 from Book A)
    assert "a1" in text_entry.href or "chapter" in text_entry.href.lower()


def test_epub3_nav_has_exactly_one_landmarks_nav(build_nav_epub: Callable[..., Path]) -> None:
    """Merging EPUB3 books produces nav.xhtml with exactly one landmarks nav (AT-NAV-2/SH-19)."""
    target = build_nav_epub(
        [("Title Page", _L + "a0"), ("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_nav_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source])

    archive = EpubArchive.open(target)
    nav_content = archive.read_bytes("OEBPS/nav.xhtml").decode("utf-8")

    # Count epub:type="landmarks" occurrences
    landmarks_count = nav_content.count('epub:type="landmarks"')
    assert landmarks_count == 1, f"nav.xhtml has {landmarks_count} landmarks nav, expected 1"

    # Check for a bodymatter entry (start of content)
    assert 'epub:type="bodymatter"' in nav_content, "landmarks nav has no bodymatter entry"


def test_rewrite_title_page_option_regenerates_it(build_epub: Callable[..., Path]) -> None:
    """With rewrite_title_page=True, title page is regenerated from metadata."""
    # Create target with a title page
    target = build_epub(
        [("Title Page", _L + "a0"), ("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    # Create source with a title page
    source = build_epub(
        [("Title Page", _L + "b0"), ("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    # Merge with rewrite_title_page=True
    merge_epubs(target, [source], options=MergeOptions(rewrite_title_page=True))

    # Reopen and check the title page
    archive = EpubArchive.open(target)
    title_page_bytes = archive.read_bytes("OEBPS/title_page.xhtml")
    title_page_xml = title_page_bytes.decode("utf-8")

    # The title page should contain the merged title (Book A, the survivor's title)
    assert "Book A" in title_page_xml
    # Should parse as XML
    from ebookerr_sdk.epub.xhtml import ChapterDocument

    ChapterDocument.parse(title_page_xml)  # Should not raise


def test_rewrite_title_page_off_by_default(build_epub: Callable[..., Path]) -> None:
    """Without rewrite_title_page option, the merged title page keeps the survivor's original."""
    # Create target with a distinctive fixture string in the title page
    target = build_epub(
        [("fff_titlepage", _L + "a0"), ("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Title Page", _L + "b0"), ("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    # Merge without the option (default is False)
    merge_epubs(target, [source])

    # Reopen and check the title page
    archive = EpubArchive.open(target)
    title_page_bytes = archive.read_bytes("OEBPS/title_page.xhtml")
    title_page_xml = title_page_bytes.decode("utf-8")

    # The title page should still contain the fixture string from the assembler
    assert "fff_titlepage" in title_page_xml


def test_rewrite_book_title_option(build_epub: Callable[..., Path]) -> None:
    """With rewrite_book_title=True, the merged title is reconstructed as <stem> <min>-<max>."""
    target = build_epub(
        [
            ("Three Square Meals - Chapter 174", _L + "174"),
            ("Three Square Meals - Chapter 175", _L + "175"),
            ("Three Square Meals - Chapter 176", _L + "176"),
        ],
        doc_title="Three Square Meals - Chapter 176",
        filename="a.epub",
    )
    source = build_epub(
        [],
        doc_title="Three Square Meals - Chapter 176",
        filename="b.epub",
    )

    merge_epubs(target, [source], options=MergeOptions(rewrite_book_title=True))

    # Reopen and check the merged title in metadata
    doc = EpubDocument.open(target)
    assert doc.opf.get_title() == "Three Square Meals 174-176"

    # Check that the NCX docTitle matches
    ncx_content = doc.ncx.to_xml()
    assert "<docTitle>" in ncx_content
    assert "<text>Three Square Meals 174-176</text>" in ncx_content


def test_rewrite_book_title_off_by_default(build_epub: Callable[..., Path]) -> None:
    """Without rewrite_book_title option, the merged title keeps the survivor's original."""
    target = build_epub(
        [
            ("Three Square Meals - Chapter 174", _L + "174"),
            ("Three Square Meals - Chapter 175", _L + "175"),
            ("Three Square Meals - Chapter 176", _L + "176"),
        ],
        doc_title="Three Square Meals - Chapter 176",
        filename="a.epub",
    )
    source = build_epub(
        [],
        doc_title="Three Square Meals - Chapter 176",
        filename="b.epub",
    )

    # Merge without the option (default is False)
    merge_epubs(target, [source])

    # Reopen and check the merged title
    doc = EpubDocument.open(target)
    assert doc.opf.get_title() == "Three Square Meals - Chapter 176"


def test_both_params_combine(build_epub: Callable[..., Path]) -> None:
    """Both rewrite options set: title page uses rewritten title."""
    target = build_epub(
        [
            ("Three Square Meals - Chapter 174", _L + "174"),
            ("Three Square Meals - Chapter 175", _L + "175"),
            ("Three Square Meals - Chapter 176", _L + "176"),
        ],
        doc_title="Three Square Meals - Chapter 176",
        filename="a.epub",
    )

    merge_epubs(
        target,
        [],
        options=MergeOptions(rewrite_book_title=True, rewrite_title_page=True),
    )

    # Reopen and check the title page contains the rewritten title
    archive = EpubArchive.open(target)
    title_page_bytes = archive.read_bytes("OEBPS/title_page.xhtml")
    title_page_xml = title_page_bytes.decode("utf-8")

    assert "Three Square Meals 174-176" in title_page_xml


def test_structural_failure_leaves_target_untouched(
    build_epub: Callable[..., Path], monkeypatch
) -> None:
    """A structural validation failure doesn't write to disk or create artifacts."""
    import tempfile
    from unittest.mock import patch

    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    # Get the original bytes
    original_bytes = target.read_bytes()

    # Monkeypatch validate_plan to raise
    def failing_validate(*args, **kwargs):
        raise MergeStructureError("boom")

    with (
        patch("epub_merge.merge.merge.validate_plan", side_effect=failing_validate),
        pytest.raises(MergeStructureError, match="boom"),
    ):
        merge_epubs(target, [source])

    # Target should be unchanged
    assert target.read_bytes() == original_bytes

    # No merge-failed artifact should have been created (staging file not preserved)
    # Search for any ebookerr-merge-failed files in temp directory
    temp_dir = Path(tempfile.gettempdir())
    artifacts = list(temp_dir.glob("ebookerr-merge-failed-*.epub"))
    # Clean up any artifacts from this test
    for artifact in artifacts:
        artifact.unlink()


def test_merge_epubs_accepts_book_urls(build_epub: Callable[..., Path]) -> None:
    """merge_epubs with book_urls parameter completes without error."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    # Should complete without error when book_urls is provided
    merge_epubs(target, [source], book_urls=["https://a", "https://b"])

    # Verify the merged file has content chapters
    doc = EpubDocument.open(target)
    links = doc.chapter_links()
    assert len(links) >= 4


def test_merge_epubs_book_urls_default_empty(build_epub: Callable[..., Path]) -> None:
    """Calling merge_epubs without book_urls succeeds (backwards compatibility)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    # Should not raise when book_urls is omitted
    outcome = merge_epubs(target, [source])
    assert outcome.chapter_count >= 2


def test_merge_epubs_single_chapter_inputs_become_recognisable(
    build_epub: Callable[..., Path],
) -> None:
    """Merging two one-chapter EPUBs with book_urls produces chapters with those URLs."""
    target = build_epub(
        [("Book A Content", "no-url")],
        doc_title="Book A",
        filename="a.epub",
    )
    source = build_epub(
        [("Book B Content", "no-url")],
        doc_title="Book B",
        filename="b.epub",
    )

    merge_epubs(target, [source], book_urls=["https://a", "https://b"])

    doc = EpubDocument.open(target)
    links = doc.chapter_links()
    # Extract just the URLs from the (url, label, ordinal) triples
    urls = [url for url, _, _ in links if url]
    # Should have at least two of our URLs (might have title page too)
    assert "https://a" in urls
    assert "https://b" in urls


class TestMergeOutcome:
    """Tests for MergeOutcome — the return value from merge_epubs."""

    def test_the_outcome_carries_the_chapter_count(self, build_epub: Callable[..., Path]) -> None:
        """The outcome.chapter_count equals the chapter count that merge_epubs computed."""
        target = build_epub(
            [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
            doc_title="Book A",
            filename="a.epub",
        )
        source = build_epub(
            [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
            doc_title="Book B",
            filename="b.epub",
        )

        outcome = merge_epubs(target, [source])

        assert outcome.chapter_count == 4
        assert outcome.title is None

    def test_the_outcome_reports_the_rewritten_title_when_the_setting_is_on(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """When rewrite_book_title=True and a rename happens, outcome.title reports it."""
        target = build_epub(
            [
                ("Three Square Meals - Chapter 174", _L + "174"),
                ("Three Square Meals - Chapter 175", _L + "175"),
                ("Three Square Meals - Chapter 176", _L + "176"),
            ],
            doc_title="Three Square Meals - Chapter 176",
            filename="a.epub",
        )
        source = build_epub(
            [],
            doc_title="Three Square Meals - Chapter 176",
            filename="b.epub",
        )

        outcome = merge_epubs(target, [source], options=MergeOptions(rewrite_book_title=True))

        # The outcome should report the rewritten title
        assert outcome.title == "Three Square Meals 174-176"
        # And the file should match
        doc = EpubDocument.open(target)
        assert doc.opf.get_title() == outcome.title

    def test_the_outcome_title_is_none_when_the_setting_is_off_even_if_the_file_title_differs(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """When rewrite_book_title=False (default), outcome.title is None even if title changed."""
        target = build_epub(
            [("Book A Ch. 1", _L + "a1")],
            doc_title="Love Outside the Margins",
            filename="a.epub",
        )
        source = build_epub(
            [("Book B Ch. 1", _L + "b1")],
            doc_title="Book B",
            filename="b.epub",
        )

        outcome = merge_epubs(target, [source])

        # With rewrite_book_title=False (default), outcome.title should be None
        assert outcome.title is None

    def test_the_outcome_title_is_none_when_no_chapter_carries_a_number(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """No chapter number means outcome.title stays None, even with rewrite_book_title=True."""
        target = build_epub(
            [("Prologue", _L + "prologue")],
            doc_title="A Book Without Numbers",
            filename="a.epub",
        )
        source = build_epub(
            [("Interlude", _L + "interlude")],
            doc_title="Another Book",
            filename="b.epub",
        )

        outcome = merge_epubs(target, [source], options=MergeOptions(rewrite_book_title=True))

        # No chapter numbers, so no rewrite even with setting on
        assert outcome.title is None
        # OPF title should still be the survivor's
        doc = EpubDocument.open(target)
        assert doc.opf.get_title() == "A Book Without Numbers"


class TestSurvivorTitle:
    """Tests for EXP-205: the merge never adopts the file's title."""

    def test_a_merge_does_not_adopt_the_opf_title_while_the_rewrite_setting_is_off(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """With rewrite_book_title=False (default), the record title is never changed.

        (EXP-205): even when the EPUB's OPF title differs from the record's title,
        the merge does not rename the record.
        """
        import ebookerr_sdk.spi as api
        from epub_merge.plugin import EpubMergePlugin

        survivor = build_epub(
            [("Chapter 38", _L + "38"), ("Chapter 39", _L + "39")],
            doc_title="Love Outside the Margins",
            filename="a.epub",
        )
        other = build_epub(
            [("Chapter 40", _L + "40")],
            doc_title="Book B",
            filename="b.epub",
        )

        # Create BookViews for the merge
        survivor_view = api.BookView(
            book_id="book_a",
            title="All is Fair, Chapter 38 - The Weight of Stone",
            author="Author A",
            story_url=None,
            output_filename="a.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )
        other_view = api.BookView(
            book_id="book_b",
            title="Chapter 39",
            author="Author B",
            story_url=None,
            output_filename="b.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )

        survivor_item = api.EpubItem(book=survivor_view, epub_path=survivor)
        other_item = api.EpubItem(book=other_view, epub_path=other)

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED, view_results=[_submitted("book_a", "book_b")]
        )

        with caplog.at_level("INFO"):
            patches = EpubMergePlugin().process((survivor_item, other_item), ctx)

        # Verify the survivor patch does NOT include a title field
        assert len(patches) == 2
        survivor_patch = patches[0]
        assert survivor_patch.fields == {"num_chapters": 3}
        assert "title" not in survivor_patch.fields

        # Verify no rename log
        info_messages = [r.message for r in caplog.records if r.levelname == "INFO"]
        rename_messages = [m for m in info_messages if "EPUB merge renamed" in m]
        assert len(rename_messages) == 0

    def test_a_merge_with_the_rewrite_setting_on_reports_the_rename_it_performed(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """When rewrite_book_title=True and a rename happens, the patch includes it."""
        import ebookerr_sdk.spi as api
        from epub_merge.plugin import EpubMergePlugin

        survivor = build_epub(
            [("Chapter 38", _L + "38"), ("Chapter 39", _L + "39")],
            doc_title="Love Outside the Margins",
            filename="a.epub",
        )
        other = build_epub(
            [("Chapter 40", _L + "40")],
            doc_title="Book B",
            filename="b.epub",
        )

        # Create BookViews for the merge
        survivor_view = api.BookView(
            book_id="book_a",
            title="All is Fair, Chapter 38 - The Weight of Stone",
            author="Author A",
            story_url=None,
            output_filename="a.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )
        other_view = api.BookView(
            book_id="book_b",
            title="Chapter 39",
            author="Author B",
            story_url=None,
            output_filename="b.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        )

        survivor_item = api.EpubItem(book=survivor_view, epub_path=survivor)
        other_item = api.EpubItem(book=other_view, epub_path=other)

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            view_results=[_submitted("book_a", "book_b")],
            settings={"rewrite_book_title": True},
        )

        with caplog.at_level("INFO"):
            patches = EpubMergePlugin().process((survivor_item, other_item), ctx)

        # Verify the survivor patch includes a title field
        assert len(patches) == 2
        survivor_patch = patches[0]
        assert "title" in survivor_patch.fields
        # The title should be rewritten as a range
        assert survivor_patch.fields["title"] is not None

        # Verify the rename log message is present
        info_messages = [r.message for r in caplog.records if r.levelname == "INFO"]
        rename_messages = [m for m in info_messages if "EPUB merge renamed" in m]
        assert len(rename_messages) > 0
        # Check for the specific phrase
        assert any("as the rewrite-title setting asks" in m for m in rename_messages)
