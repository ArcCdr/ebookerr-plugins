"""Tests for EPUB cover image and cover page planning."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from ebookerr_sdk.epub.assets import AssetRegistry
from ebookerr_sdk.epub.xhtml import ChapterDocument
from epub_merge.merge.assets import plan_assets
from epub_merge.merge.cover import plan_cover, render_cover_page
from epub_merge.merge.errors import MergeInputError
from epub_merge.merge.model import (
    InputBook,
    InputChapter,
    InputResource,
    MergeOptions,
    PlannedChapter,
)
from epub_merge.merge.pages import apply_title_page
from epub_merge.merge.plan import build_merge_plan
from epub_merge.merge.validate import validate_plan


def _book(
    index: int,
    *labels: str,
    resources: tuple[InputResource, ...] = (),
    cover_href: str | None = None,
    cover_media_type: str | None = None,
    content_root: str = "OEBPS",
) -> InputBook:
    """Build a minimal InputBook for cover testing."""
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
        version="2.0",
        title=f"Book {index}",
        creators=(),
        contributors=(),
        language="en",
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
        stylesheets=(),
        cover_href=cover_href,
        cover_media_type=cover_media_type,
    )


def test_no_cover_when_absent_everywhere(caplog: pytest.LogCaptureFixture) -> None:
    """No cover option and survivor has no cover_href → plan returns None (AT-COVER-2)."""
    books = [_book(0, "Chapter 1")]
    assets, registry = plan_assets(books)

    with caplog.at_level(logging.DEBUG):
        result = plan_cover(books, MergeOptions(), assets, registry=registry)

    assert result is None
    assert "Merge produced no cover" in caplog.text


def test_survivor_cover_is_carried_over() -> None:
    """Survivor has cover_href and it's in assets.mapping → plan reuses it (AT-COVER-3)."""
    cover_resource = InputResource(
        href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
    )
    books = [
        _book(
            0,
            "Chapter 1",
            resources=(cover_resource,),
            cover_href="OEBPS/cover.jpg",
            cover_media_type="image/jpeg",
        )
    ]
    assets, registry = plan_assets(books)

    result = plan_cover(books, MergeOptions(), assets, registry=registry)

    assert result is not None
    assert result.image_path == "cover.jpg"
    assert result.image_media_type == "image/jpeg"
    assert result.extra_files == {}


def test_option_overrides_survivor_cover(tmp_path: Path) -> None:
    """cover_image option set → use it, ignoring survivor cover."""
    cover_file = tmp_path / "my_cover.png"
    cover_file.write_bytes(b"\x89PNG")

    cover_resource = InputResource(
        href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
    )
    books = [
        _book(
            0,
            "Chapter 1",
            resources=(cover_resource,),
            cover_href="OEBPS/cover.jpg",
            cover_media_type="image/jpeg",
        )
    ]
    assets, registry = plan_assets(books)

    result = plan_cover(books, MergeOptions(cover_image=cover_file), assets, registry=registry)

    assert result is not None
    assert result.image_path == "cover.png"
    assert result.extra_files == {"cover.png": b"\x89PNG"}


def test_option_media_type_from_extension(tmp_path: Path) -> None:
    """File extensions map to correct media types."""
    cases = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
    }
    for ext, expected_type in cases.items():
        tmp_file = tmp_path / f"test_cover{ext}"
        tmp_file.write_bytes(b"fake")
        assets, registry = plan_assets([_book(0, "Ch")])
        result = plan_cover(
            [_book(0, "Ch")],
            MergeOptions(cover_image=tmp_file),
            assets,
            registry=registry,
        )
        assert result is not None
        assert result.image_media_type == expected_type


def test_option_unknown_extension_defaults_to_jpeg(tmp_path: Path) -> None:
    """Unknown extension defaults to image/jpeg."""
    cover_file = tmp_path / "cover.bin"
    cover_file.write_bytes(b"unknown")

    assets, registry = plan_assets([_book(0, "Ch")])
    result = plan_cover(
        [_book(0, "Ch")],
        MergeOptions(cover_image=cover_file),
        assets,
        registry=registry,
    )

    assert result is not None
    assert result.image_media_type == "image/jpeg"
    assert result.image_path == "cover.bin"


def test_unreadable_option_raises(tmp_path: Path) -> None:
    """Missing cover_image file raises MergeInputError."""
    missing = tmp_path / "missing.png"

    assets, registry = plan_assets([_book(0, "Ch")])
    with pytest.raises(MergeInputError, match='cover image ".*missing.png" could not be read'):
        plan_cover(
            [_book(0, "Ch")],
            MergeOptions(cover_image=missing),
            assets,
            registry=registry,
        )


def test_survivor_cover_without_mapping_yields_none() -> None:
    """Survivor has cover_href but no assets.mapping entry → return None."""
    # Create a survivor with a cover_href that won't be in assets.mapping
    books = [
        _book(
            0,
            "Chapter 1",
            cover_href="OEBPS/missing.jpg",
            cover_media_type="image/jpeg",
        )
    ]
    # No cover resource added, so mapping won't have the entry
    assets, registry = plan_assets(books)

    result = plan_cover(books, MergeOptions(), assets, registry=registry)

    assert result is None


def test_cover_page_is_valid_xml() -> None:
    """render_cover_page produces valid XHTML."""
    page_bytes = render_cover_page("cover.jpg")
    # Verify the document is valid XML by parsing it
    ChapterDocument.parse(page_bytes.decode())

    # Check that the document contains the image reference
    xml_str = page_bytes.decode()
    assert 'src="cover.jpg"' in xml_str
    assert 'alt="Cover"' in xml_str


def test_cover_page_has_no_external_references() -> None:
    """Rendered cover page has no http/https/CDN references."""
    page_bytes = render_cover_page("cover.jpg")
    xml_str = page_bytes.decode()

    # Check for external content references, excluding namespace URIs
    assert "https://example.com" not in xml_str
    assert "//cdn" not in xml_str
    # The xmlns namespace URL is allowed
    assert 'xmlns="http://www.w3.org/1999/xhtml"' in xml_str


def test_logs_info_for_each_source(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Logging distinguishes option vs survivor sources."""
    # Test option source
    tmp_file = tmp_path / "test_cover_log.png"
    tmp_file.write_bytes(b"\x89PNG")
    assets, registry = plan_assets([_book(0, "Ch")])
    with caplog.at_level(logging.INFO):
        plan_cover(
            [_book(0, "Ch")],
            MergeOptions(cover_image=tmp_file),
            assets,
            registry=registry,
        )
    assert "from the manifest option" in caplog.text

    caplog.clear()

    # Test survivor source
    cover_resource = InputResource(
        href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
    )
    books = [
        _book(
            0,
            "Chapter 1",
            resources=(cover_resource,),
            cover_href="OEBPS/cover.jpg",
            cover_media_type="image/jpeg",
        )
    ]
    assets, registry = plan_assets(books)
    with caplog.at_level(logging.INFO):
        plan_cover(books, MergeOptions(), assets, registry=registry)
    assert "carried over from the survivor" in caplog.text


class TestCoverPagePathReservation:
    """Tests for cover page path reservation to avoid collisions with carried assets."""

    def test_cover_page_path_avoids_a_carried_cover_xhtml(self) -> None:
        """Synthesised cover page avoids carried cover.xhtml, uses cover_1.xhtml."""
        # Create survivor with cover.xhtml as a resource
        cover_xhtml_resource = InputResource(
            href="OEBPS/cover.xhtml", media_type="application/xhtml+xml", data=b"<html>cover</html>"
        )
        cover_image = InputResource(
            href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
        )
        books = [
            _book(
                0,
                "Chapter 1",
                resources=(cover_xhtml_resource, cover_image),
                cover_href="OEBPS/cover.jpg",
                cover_media_type="image/jpeg",
            )
        ]

        # Create a registry with cover.xhtml already claimed
        registry = AssetRegistry()
        registry.claim("cover.xhtml", b"<html>cover</html>")

        # plan_cover with registry should avoid cover.xhtml
        assets, _ = plan_assets(books)
        result = plan_cover(books, MergeOptions(), assets, registry=registry)

        assert result is not None
        assert result.page_path == "cover_1.xhtml"

    def test_cover_page_keeps_plain_path_when_free(self) -> None:
        """When cover.xhtml is free, synthesised cover page uses it."""
        cover_image = InputResource(
            href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
        )
        books = [
            _book(
                0,
                "Chapter 1",
                resources=(cover_image,),
                cover_href="OEBPS/cover.jpg",
                cover_media_type="image/jpeg",
            )
        ]

        registry = AssetRegistry()
        assets, _ = plan_assets(books)
        result = plan_cover(books, MergeOptions(), assets, registry=registry)

        assert result is not None
        assert result.page_path == "cover.xhtml"

    def test_title_page_path_avoids_a_carried_title_page(self) -> None:
        """Synthesised title page avoids carried title_page.xhtml, uses title_page_1.xhtml."""
        # Create a registry with title_page.xhtml already claimed
        registry = AssetRegistry()
        registry.claim("title_page.xhtml", b"<html>title</html>")

        # Create planned chapters
        planned_chapters = [
            PlannedChapter(
                label="Chapter 1",
                filename="file0001.xhtml",
                item_id="file0001",
                number=None,
                book_index=0,
                source_href="OEBPS/file0001.xhtml",
                xhtml=b"<html>content</html>",
            )
        ]

        # apply_title_page with registry should avoid title_page.xhtml
        result_chapters = apply_title_page(
            planned_chapters,
            title="Test Title",
            creators=[],
            subjects=[],
            language="en",
            stylesheets=[],
            registry=registry,
        )

        # The title page should have been inserted or replaced at index 0
        title_chapter = result_chapters[0]
        assert title_chapter.filename == "title_page_1.xhtml"

    def test_merged_manifest_has_no_duplicate_href(self) -> None:
        """A full merge plan with collision fixture should have no duplicate hrefs."""
        # Create survivor with cover.xhtml as a resource AND cover_href pointing to a cover image
        cover_xhtml_resource = InputResource(
            href="OEBPS/cover.xhtml", media_type="application/xhtml+xml", data=b"<html>cover</html>"
        )
        cover_image = InputResource(
            href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
        )
        survivor_chapters = (
            InputChapter(
                label="Chapter 1",
                href="OEBPS/file0001.xhtml",
                item_id="file0001",
                media_type="application/xhtml+xml",
                xhtml=b"<html>chapter 1</html>",
            ),
            InputChapter(
                label="Chapter 2",
                href="OEBPS/file0002.xhtml",
                item_id="file0002",
                media_type="application/xhtml+xml",
                xhtml=b"<html>chapter 2</html>",
            ),
        )
        survivor = InputBook(
            index=0,
            name="survivor.epub",
            version="2.0",
            title="Survivor Book",
            creators=(),
            contributors=(),
            language="en",
            identifier="id-survivor",
            source=None,
            rights=None,
            publisher=None,
            subjects=(),
            dates=(),
            page_direction="ltr",
            content_root="OEBPS",
            chapters=survivor_chapters,
            resources=(cover_xhtml_resource, cover_image),
            stylesheets=(),
            cover_href="OEBPS/cover.jpg",
            cover_media_type="image/jpeg",
        )

        # Create source book with one chapter
        source_chapters = (
            InputChapter(
                label="Chapter 3",
                href="OEBPS/file0001.xhtml",
                item_id="file0001",
                media_type="application/xhtml+xml",
                xhtml=b"<html>chapter 3</html>",
            ),
        )
        source = InputBook(
            index=1,
            name="source.epub",
            version="2.0",
            title="Source Book",
            creators=(),
            contributors=(),
            language="en",
            identifier="id-source",
            source=None,
            rights=None,
            publisher=None,
            subjects=(),
            dates=(),
            page_direction="ltr",
            content_root="OEBPS",
            chapters=source_chapters,
            resources=(),
            stylesheets=(),
            cover_href=None,
            cover_media_type=None,
        )

        books = [survivor, source]
        plan = build_merge_plan(books, MergeOptions(rewrite_title_page=True))

        # Extract all hrefs from the manifest
        hrefs = [item.href for item in plan.package.manifest]
        unique_hrefs = set(hrefs)

        assert len(hrefs) == len(unique_hrefs), f"Duplicate hrefs found: {hrefs}"

    def test_collision_fixture_validates(self) -> None:
        """The full merge plan with collision should validate without errors."""
        # Create survivor with cover.xhtml as a resource AND cover_href pointing to a cover image
        cover_xhtml_resource = InputResource(
            href="OEBPS/cover.xhtml", media_type="application/xhtml+xml", data=b"<html>cover</html>"
        )
        cover_image = InputResource(
            href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
        )
        survivor_chapters = (
            InputChapter(
                label="Chapter 1",
                href="OEBPS/file0001.xhtml",
                item_id="file0001",
                media_type="application/xhtml+xml",
                xhtml=b"<html>chapter 1</html>",
            ),
        )
        survivor = InputBook(
            index=0,
            name="survivor.epub",
            version="2.0",
            title="Survivor Book",
            creators=(),
            contributors=(),
            language="en",
            identifier="id-survivor",
            source=None,
            rights=None,
            publisher=None,
            subjects=(),
            dates=(),
            page_direction="ltr",
            content_root="OEBPS",
            chapters=survivor_chapters,
            resources=(cover_xhtml_resource, cover_image),
            stylesheets=(),
            cover_href="OEBPS/cover.jpg",
            cover_media_type="image/jpeg",
        )

        # Create source book with one chapter
        source_chapters = (
            InputChapter(
                label="Chapter 2",
                href="OEBPS/file0001.xhtml",
                item_id="file0001",
                media_type="application/xhtml+xml",
                xhtml=b"<html>chapter 2</html>",
            ),
        )
        source = InputBook(
            index=1,
            name="source.epub",
            version="2.0",
            title="Source Book",
            creators=(),
            contributors=(),
            language="en",
            identifier="id-source",
            source=None,
            rights=None,
            publisher=None,
            subjects=(),
            dates=(),
            page_direction="ltr",
            content_root="OEBPS",
            chapters=source_chapters,
            resources=(),
            stylesheets=(),
            cover_href=None,
            cover_media_type=None,
        )

        books = [survivor, source]
        plan = build_merge_plan(books, MergeOptions(rewrite_title_page=True))

        # Should not raise MergeStructureError
        # Title page is not counted as a chapter in spine, so expect only content chapters
        # survivor has 1 chapter, source has 1 chapter
        validate_plan(plan, expected_chapters=2)

    def test_cover_reservation_is_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """When cover path is reserved with a suffix, it logs at DEBUG level."""
        # Create survivor with cover.xhtml as a resource AND cover_href pointing to a cover image
        cover_xhtml_resource = InputResource(
            href="OEBPS/cover.xhtml", media_type="application/xhtml+xml", data=b"<html>cover</html>"
        )
        cover_image = InputResource(
            href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
        )
        books = [
            _book(
                0,
                "Chapter 1",
                resources=(cover_xhtml_resource, cover_image),
                cover_href="OEBPS/cover.jpg",
                cover_media_type="image/jpeg",
            )
        ]

        # Create a registry with cover.xhtml already claimed
        registry = AssetRegistry()
        registry.claim("cover.xhtml", b"<html>cover</html>")

        assets, _ = plan_assets(books)
        with caplog.at_level(logging.DEBUG, logger="epub_merge.merge.cover"):
            result = plan_cover(books, MergeOptions(), assets, registry=registry)

        assert result is not None
        assert "Merge cover page reserved a free path" in caplog.text
