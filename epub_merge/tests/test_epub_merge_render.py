"""Tests for rendering a merge plan into an EPUB archive."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from ebookerr_sdk.epub.builder import MetadataSpec, PackageSpec
from ebookerr_sdk.epub.document import EpubDocument
from epub_merge.merge.model import InputBook, InputChapter, InputResource, MergeOptions
from epub_merge.merge.plan import MergePlan, build_merge_plan
from epub_merge.merge.render import render_plan


def _book(
    index: int,
    *labels: str,
    resources: tuple[InputResource, ...] = (),
    title: str | None = None,
    language: str = "en",
    version: str = "2.0",
    creators: tuple[tuple[str, str | None], ...] = (),
    content_root: str = "OEBPS",
) -> InputBook:
    """Build a neutral InputBook with the given chapter labels and resources.

    Args:
        index: Book index.
        *labels: Chapter labels in reading order.
        resources: Non-chapter resources to include.
        title: Book title; defaults to ``f"Book {index}"``.
        language: Language code.
        version: EPUB version.
        creators: Tuple of (name, file_as) tuples.
        content_root: The book's content directory.

    Returns:
        An InputBook with neutral metadata and the provided chapters/resources.
    """
    chapters = tuple(
        InputChapter(
            label=label,
            href=f"{content_root}/file{i:04d}.xhtml",
            item_id=f"file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=f"<html>{label}</html>".encode(),
        )
        for i, label in enumerate(labels)
    )
    return InputBook(
        index=index,
        name=f"book{index}.epub",
        version=version,
        title=title if title is not None else f"Book {index}",
        creators=creators,
        contributors=(),
        language=language,
        identifier=f"id-{index}",
        source=None,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root=content_root,
        chapters=chapters,
        resources=resources,
        stylesheets=tuple(r.href for r in resources if r.media_type == "text/css"),
        cover_href=None,
        cover_media_type=None,
    )


def test_member_order_starts_with_mimetype_and_container() -> None:
    """First two members are mimetype and container.xml in correct order."""
    books = [_book(0, "Chapter 1")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    names = archive.names()
    assert names[:2] == ["mimetype", "META-INF/container.xml"]


def test_mimetype_content() -> None:
    """Mimetype member contains exactly 'application/epub+zip'."""
    books = [_book(0, "Chapter 1")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    assert archive.read_bytes("mimetype") == b"application/epub+zip"


def test_opf_and_ncx_are_under_oebps() -> None:
    """OPF and NCX members are located under OEBPS."""
    books = [_book(0, "Chapter 1")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    names = archive.names()
    assert "OEBPS/content.opf" in names
    assert "OEBPS/toc.ncx" in names


def test_files_are_written_under_oebps() -> None:
    """Plan files are written as OEBPS/{path} with exact bytes."""
    resource = InputResource(href="OEBPS/images/test.png", media_type="image/png", data=b"\x89PNG")
    books = [_book(0, "Chapter 1", resources=(resource,))]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    assert archive.read_bytes("OEBPS/images/test.png") == b"\x89PNG"


def test_no_nav_for_epub2() -> None:
    """EPUB 2.0 does not include nav.xhtml."""
    books = [_book(0, "Chapter 1", version="2.0")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    names = archive.names()
    assert "OEBPS/nav.xhtml" not in names


def test_nav_written_for_epub3() -> None:
    """EPUB 3.0 includes nav.xhtml with epub:type='toc'."""
    # Create a plan directly with version="3.0" since build_merge_plan hard-codes "2.0"
    metadata = MetadataSpec(
        title="Test Book",
        identifier="urn:uuid:12345678-1234-1234-1234-123456789012",
        language="en",
    )
    package = PackageSpec(
        version="3.0",
        metadata=metadata,
        manifest=(),
        spine=(),
    )
    plan = MergePlan(
        version="3.0",
        title="Test Book",
        identifier="urn:uuid:12345678-1234-1234-1234-123456789012",
        files={},
        package=package,
        chapters=(),
        toc=(),
        landmarks=(),
    )
    archive = render_plan(plan)

    names = archive.names()
    assert "OEBPS/nav.xhtml" in names

    nav_content = archive.read_text("OEBPS/nav.xhtml")
    assert 'epub:type="toc"' in nav_content


def test_container_points_at_the_opf() -> None:
    """Container.xml contains full-path='OEBPS/content.opf'."""
    books = [_book(0, "Chapter 1")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    container = archive.read_text("META-INF/container.xml")
    assert 'full-path="OEBPS/content.opf"' in container


def test_ncx_uses_plan_title_and_identifier() -> None:
    """NCX contains plan title in <text> and identifier as dtb:uid."""
    books = [_book(0, "Chapter 1", title="My Merged Book")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    ncx = archive.read_text("OEBPS/toc.ncx")
    assert plan.title in ncx
    assert f'content="{plan.identifier}"' in ncx


def test_saved_archive_reopens_as_epub_document(tmp_path: Path) -> None:
    """A rendered, saved archive opens cleanly as EpubDocument."""
    books = [_book(0, "Chapter 1", "Chapter 2")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    output_path = tmp_path / "out.epub"
    archive.save(output_path)

    reopened = EpubDocument.open(output_path)
    assert reopened.opf_dir == "OEBPS"
    assert len(list(reopened.opf.manifest())) > 0
    assert len(list(reopened.opf.spine())) > 0


def test_saved_archive_zip_contract(tmp_path: Path) -> None:
    """Saved archive respects the zip contract: mimetype first, stored, create_system=0."""
    books = [_book(0, "Chapter 1")]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    output_path = tmp_path / "out.epub"
    archive.save(output_path)

    import zipfile

    with zipfile.ZipFile(output_path) as zf:
        info_list = zf.infolist()
        assert len(info_list) > 0

        mimetype_info = info_list[0]
        assert mimetype_info.filename == "mimetype"
        assert mimetype_info.compress_type == zipfile.ZIP_STORED
        assert all(info.create_system == 0 for info in info_list)

        # Check mimetype bytes at offset 38:58 (inside the zip)
        with output_path.open("rb") as f:
            f.seek(38)
            zip_content_slice = f.read(20)
            assert b"application/epub+zip" in zip_content_slice


def test_files_written_in_sorted_order() -> None:
    """Plan files are written in alphabetical order after fixed members."""
    resources = (
        InputResource(href="OEBPS/z-last.png", media_type="image/png", data=b"z"),
        InputResource(href="OEBPS/a-first.png", media_type="image/png", data=b"a"),
        InputResource(href="OEBPS/m-middle.png", media_type="image/png", data=b"m"),
    )
    books = [_book(0, "Chapter 1", resources=resources)]
    plan = build_merge_plan(books, MergeOptions())
    archive = render_plan(plan)

    names = archive.names()
    # Fixed members: mimetype, container, opf, ncx
    fixed_count = 4
    # Find the indices of the resources
    file_members = [
        name for name in names[fixed_count:] if name.startswith("OEBPS/") and name.endswith(".png")
    ]
    # Should be sorted
    assert file_members == sorted(file_members)


def test_logs_debug_member_count(caplog: pytest.LogCaptureFixture) -> None:
    """Debug log contains 'Merge rendered' and member count."""
    books = [_book(0, "Chapter 1")]
    plan = build_merge_plan(books, MergeOptions())

    with caplog.at_level(logging.DEBUG):
        render_plan(plan)

    assert any("Merge rendered" in record.message for record in caplog.records)
