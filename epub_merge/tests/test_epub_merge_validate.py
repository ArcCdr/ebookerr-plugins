"""Tests for `validate_plan` — structural self-check of a merged EPUB plan."""

from __future__ import annotations

import pytest
from ebookerr_sdk.epub.builder import (
    ManifestEntry,
    MetadataSpec,
    PackageSpec,
    SpineEntry,
    TocEntry,
)
from epub_merge.merge.errors import MergeContentError, MergeStructureError
from epub_merge.merge.plan import MergePlan


def _plan(
    *,
    manifest: list[ManifestEntry] | None = None,
    spine: list[SpineEntry] | None = None,
    toc: list[TocEntry] | None = None,
    files: dict[str, bytes] | None = None,
    title: str = "Test Book",
    version: str = "2.0",
    identifier: str = "urn:uuid:test",
) -> MergePlan:
    """Build a minimal valid MergePlan with sensible defaults.

    Allows easy mutation of individual aspects for testing.

    Args:
        manifest: Manifest entries; defaults to a single XHTML chapter + ncx.
        spine: Spine entries; defaults to a single chapter.
        toc: TOC entries; defaults to a single chapter.
        files: File content dict; defaults to chapter and ncx.
        title: Book title.
        version: EPUB version.
        identifier: Unique identifier.

    Returns:
        A MergePlan with the given or default values.
    """
    if manifest is None:
        manifest = [
            ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
            ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
        ]
    if spine is None:
        spine = [SpineEntry("ch1")]
    if toc is None:
        toc = [TocEntry("Chapter 1", "chapter_1.xhtml")]
    if files is None:
        files = {"chapter_1.xhtml": b"<html></html>", "toc.ncx": b"<ncx></ncx>"}

    metadata = MetadataSpec(
        title=title,
        identifier=identifier,
    )
    package = PackageSpec(
        version=version,
        metadata=metadata,
        manifest=tuple(manifest),
        spine=tuple(spine),
    )

    return MergePlan(
        version=version,
        title=title,
        identifier=identifier,
        files=files,
        package=package,
        chapters=(),
        toc=tuple(toc),
        landmarks=(),
    )


def test_valid_plan_passes(caplog) -> None:
    """A well-formed plan raises nothing and logs the DEBUG summary."""
    import logging

    from epub_merge.merge.validate import validate_plan

    caplog.set_level(logging.DEBUG)
    plan = _plan()

    # Should not raise
    validate_plan(plan, expected_chapters=1)

    # Should log a debug message
    assert any("Merge plan validated" in record.message for record in caplog.records)


def test_duplicate_manifest_id_raises() -> None:
    """Two entries sharing an item_id raise MergeStructureError."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch1", "chapter_2.xhtml", "application/xhtml+xml"),
    ]
    plan = _plan(manifest=manifest)

    with pytest.raises(MergeStructureError, match="duplicate manifest id"):
        validate_plan(plan, expected_chapters=2)


def test_duplicate_manifest_href_raises() -> None:
    """Two entries sharing an href raise MergeStructureError."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch2", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    plan = _plan(manifest=manifest)

    with pytest.raises(MergeStructureError, match="duplicate manifest href"):
        validate_plan(plan, expected_chapters=2)


def test_duplicate_spine_idref_raises() -> None:
    """Two spine entries referencing the same idref raise MergeStructureError."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("ch1"), SpineEntry("ch1")]
    plan = _plan(manifest=manifest, spine=spine)

    with pytest.raises(MergeStructureError, match="duplicate spine idref"):
        validate_plan(plan, expected_chapters=1)


def test_duplicate_toc_href_raises() -> None:
    """Two TOC entries with the same href raise MergeStructureError."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    toc = [
        TocEntry("Chapter 1", "chapter_1.xhtml"),
        TocEntry("Chapter 1 (again)", "chapter_1.xhtml"),
    ]
    plan = _plan(manifest=manifest, toc=toc)

    with pytest.raises(MergeStructureError, match="duplicate TOC"):
        validate_plan(plan, expected_chapters=1)


def test_spine_without_manifest_entry_raises() -> None:
    """A spine idref that does not match any manifest item_id raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("ch1"), SpineEntry("unknown")]
    plan = _plan(manifest=manifest, spine=spine)

    with pytest.raises(MergeStructureError, match="unknown manifest item"):
        validate_plan(plan, expected_chapters=2)


def test_toc_href_without_manifest_entry_raises() -> None:
    """A TOC href that does not match any manifest href raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    toc = [
        TocEntry("Chapter 1", "chapter_1.xhtml"),
        TocEntry("Chapter 2", "unknown.xhtml"),
    ]
    plan = _plan(manifest=manifest, toc=toc)

    with pytest.raises(MergeStructureError, match="TOC entry"):
        validate_plan(plan, expected_chapters=2)


def test_invalid_ncname_raises() -> None:
    """An item_id that is not a valid XML NCName raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("1chapter", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("1chapter")]
    toc = [TocEntry("Chapter 1", "chapter_1.xhtml")]
    plan = _plan(manifest=manifest, spine=spine, toc=toc)

    with pytest.raises(MergeStructureError, match="not a valid XML name"):
        validate_plan(plan, expected_chapters=1)


def test_xhtml_href_with_slash_raises() -> None:
    """An XHTML manifest entry with a `/` in its href raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "text/chapter_1.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("ch1")]
    toc = [TocEntry("Chapter 1", "text/chapter_1.xhtml")]
    plan = _plan(manifest=manifest, spine=spine, toc=toc)

    with pytest.raises(MergeStructureError, match="must be flat"):
        validate_plan(plan, expected_chapters=1)


def test_resource_href_with_slash_is_allowed() -> None:
    """A non-XHTML manifest entry (like an image) can have `/` in its href."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
        ManifestEntry("img1", "images/a.png", "image/png"),
    ]
    spine = [SpineEntry("ch1")]
    toc = [TocEntry("Chapter 1", "chapter_1.xhtml")]
    files = {
        "chapter_1.xhtml": b"<html></html>",
        "toc.ncx": b"<ncx></ncx>",
        "images/a.png": b"PNG",
    }
    plan = _plan(manifest=manifest, spine=spine, toc=toc, files=files)

    # Should not raise
    validate_plan(plan, expected_chapters=1)


def test_merged_substring_raises() -> None:
    """A file named with 'merged' (case-insensitive) raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "merged_1.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("ch1")]
    toc = [TocEntry("Chapter 1", "merged_1.xhtml")]
    plan = _plan(manifest=manifest, spine=spine, toc=toc)

    with pytest.raises(MergeStructureError, match="merged"):
        validate_plan(plan, expected_chapters=1)


def test_manifest_entry_without_file_raises() -> None:
    """A manifest href not in plan.files (except toc.ncx/nav.xhtml) raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch2", "chapter_2.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("ch1"), SpineEntry("ch2")]
    toc = [
        TocEntry("Chapter 1", "chapter_1.xhtml"),
        TocEntry("Chapter 2", "chapter_2.xhtml"),
    ]
    files = {"chapter_1.xhtml": b"<html></html>", "toc.ncx": b"<ncx></ncx>"}
    plan = _plan(manifest=manifest, spine=spine, toc=toc, files=files)

    with pytest.raises(MergeStructureError, match="has no file"):
        validate_plan(plan, expected_chapters=2)


def test_orphan_file_raises() -> None:
    """A plan.files key not in the manifest raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("ch1")]
    toc = [TocEntry("Chapter 1", "chapter_1.xhtml")]
    files = {
        "chapter_1.xhtml": b"<html></html>",
        "toc.ncx": b"<ncx></ncx>",
        "orphan.xhtml": b"<html></html>",
    }
    plan = _plan(manifest=manifest, spine=spine, toc=toc, files=files)

    with pytest.raises(MergeStructureError, match="not in the manifest"):
        validate_plan(plan, expected_chapters=1)


def test_ncx_and_nav_are_exempt_from_the_file_check() -> None:
    """toc.ncx and nav.xhtml may be in the manifest without being in files."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("nav", "nav.xhtml", "application/xhtml+xml", properties="nav"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("nav"), SpineEntry("ch1")]
    toc = [TocEntry("Chapter 1", "chapter_1.xhtml")]
    files = {"chapter_1.xhtml": b"<html></html>"}
    plan = _plan(manifest=manifest, spine=spine, toc=toc, files=files)

    # Should not raise
    validate_plan(plan, expected_chapters=1)


def test_too_few_chapters_raises() -> None:
    """A plan with fewer chapters than expected raises."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch2", "chapter_2.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch3", "chapter_3.xhtml", "application/xhtml+xml"),
    ]
    spine = [SpineEntry("ch1"), SpineEntry("ch2"), SpineEntry("ch3")]
    toc = [
        TocEntry("Chapter 1", "chapter_1.xhtml"),
        TocEntry("Chapter 2", "chapter_2.xhtml"),
        TocEntry("Chapter 3", "chapter_3.xhtml"),
    ]
    files = {
        "chapter_1.xhtml": b"<html></html>",
        "chapter_2.xhtml": b"<html></html>",
        "chapter_3.xhtml": b"<html></html>",
        "toc.ncx": b"<ncx></ncx>",
    }
    plan = _plan(manifest=manifest, spine=spine, toc=toc, files=files)

    with pytest.raises(MergeContentError, match="expected at least 5"):
        validate_plan(plan, expected_chapters=5)


def test_non_chapter_spine_entries_do_not_count() -> None:
    """Spine entries with non-chapter item_ids don't count toward expected_chapters."""
    from epub_merge.merge.validate import validate_plan

    manifest = [
        ManifestEntry("ncx", "toc.ncx", "application/x-dtbncx+xml"),
        ManifestEntry("cover", "cover.xhtml", "application/xhtml+xml"),
        ManifestEntry("title_page", "title.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch1", "chapter_1.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch2", "chapter_2.xhtml", "application/xhtml+xml"),
        ManifestEntry("ch3", "chapter_3.xhtml", "application/xhtml+xml"),
    ]
    spine = [
        SpineEntry("cover"),
        SpineEntry("title_page"),
        SpineEntry("ch1"),
        SpineEntry("ch2"),
        SpineEntry("ch3"),
    ]
    toc = [
        TocEntry("Chapter 1", "chapter_1.xhtml"),
        TocEntry("Chapter 2", "chapter_2.xhtml"),
        TocEntry("Chapter 3", "chapter_3.xhtml"),
    ]
    files = {
        "cover.xhtml": b"<html></html>",
        "title.xhtml": b"<html></html>",
        "chapter_1.xhtml": b"<html></html>",
        "chapter_2.xhtml": b"<html></html>",
        "chapter_3.xhtml": b"<html></html>",
        "toc.ncx": b"<ncx></ncx>",
    }
    plan = _plan(manifest=manifest, spine=spine, toc=toc, files=files)

    # Should not raise, since the 3 actual chapters >= 3 expected
    validate_plan(plan, expected_chapters=3)
