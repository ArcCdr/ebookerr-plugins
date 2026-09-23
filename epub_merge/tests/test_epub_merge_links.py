"""Tests for epub_merge.merge.links — chapter reference rewriting."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from ebookerr_sdk.epub import EpubArchive, EpubDocument
from epub_merge.merge.assets import AssetPlan
from epub_merge.merge.links import rewrite_chapter_links
from epub_merge.merge.model import PlannedChapter


def _make_chapter(
    label: str,
    filename: str,
    item_id: str,
    source_href: str,
    xhtml: str,
    book_index: int = 0,
    number: int | None = None,
) -> PlannedChapter:
    """Helper to create a PlannedChapter from XHTML string."""
    return PlannedChapter(
        label=label,
        filename=filename,
        item_id=item_id,
        number=number,
        book_index=book_index,
        source_href=source_href,
        xhtml=xhtml.encode("utf-8"),
    )


class TestRewriteChapterLinks:
    """Test rewrite_chapter_links behavior on various reference patterns."""

    def test_rewrites_relative_image_path(self) -> None:
        """A chapter at OEBPS/text/ch1.xhtml with ../images/divider.png is rewritten."""
        xhtml = '<?xml version="1.0"?><html><body><img src="../images/divider.png"/></body></html>'
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/text/ch1.xhtml",
            xhtml=xhtml,
        )
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={(0, "OEBPS/images/divider.png"): "images/divider.png"},
        )

        result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        xml = result[0].xhtml.decode("utf-8")
        assert 'src="images/divider.png"' in xml

    def test_rewrites_sibling_image_path(self) -> None:
        """Chapter at OEBPS/ch1.xhtml with pic.png stays unchanged if mapping matches."""
        xhtml = '<?xml version="1.0"?><html><body><img src="pic.png"/></body></html>'
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml,
        )
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={(0, "OEBPS/pic.png"): "pic.png"},
        )

        result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        # The path doesn't change, so bytes should match
        assert result[0].xhtml == chapter.xhtml

    def test_rewrites_renamed_asset(self) -> None:
        """An asset remapped to a different name is reflected in the chapter."""
        xhtml = '<?xml version="1.0"?><html><body><img src="../images/divider.png"/></body></html>'
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/text/ch1.xhtml",
            xhtml=xhtml,
        )
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={(0, "OEBPS/images/divider.png"): "images/divider_1.png"},
        )

        result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        xml = result[0].xhtml.decode("utf-8")
        assert 'src="images/divider_1.png"' in xml

    def test_rewrites_cross_chapter_link(self) -> None:
        """Cross-chapter link to ch2.xhtml rewritten to planned filename."""
        xhtml1 = '<?xml version="1.0"?><html><body><a href="ch2.xhtml">Next</a></body></html>'
        xhtml2 = '<?xml version="1.0"?><html><body>Chapter 2</body></html>'
        ch1 = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml1,
        )
        ch2 = _make_chapter(
            label="Chapter 2",
            filename="chapter_002_book.xhtml",
            item_id="chapter_002_book",
            source_href="OEBPS/ch2.xhtml",
            xhtml=xhtml2,
        )
        assets = AssetPlan(files={}, media_types={}, mapping={})

        result = rewrite_chapter_links([ch1, ch2], assets)

        assert len(result) == 2
        xml = result[0].xhtml.decode("utf-8")
        assert 'href="chapter_002_book.xhtml"' in xml

    def test_preserves_fragment_on_cross_chapter_link(self) -> None:
        """Fragment identifiers preserved when rewriting cross-chapter links."""
        xhtml1 = (
            '<?xml version="1.0"?>'
            '<html><body><a href="ch2.xhtml#part3">Go to Part 3</a></body></html>'
        )
        xhtml2 = '<?xml version="1.0"?><html><body>Chapter 2</body></html>'
        ch1 = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml1,
        )
        ch2 = _make_chapter(
            label="Chapter 2",
            filename="chapter_002_book.xhtml",
            item_id="chapter_002_book",
            source_href="OEBPS/ch2.xhtml",
            xhtml=xhtml2,
        )
        assets = AssetPlan(files={}, media_types={}, mapping={})

        result = rewrite_chapter_links([ch1, ch2], assets)

        assert len(result) == 2
        xml = result[0].xhtml.decode("utf-8")
        assert 'href="chapter_002_book.xhtml#part3"' in xml

    def test_leaves_external_links_alone(self) -> None:
        """External URLs (with scheme or //) are not rewritten."""
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/ch1.xhtml",
            xhtml='<?xml version="1.0"?><html><body><a href="https://example.com">External</a></body></html>',
        )
        assets = AssetPlan(files={}, media_types={}, mapping={})

        result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        xml = result[0].xhtml.decode("utf-8")
        assert 'href="https://example.com"' in xml

    def test_leaves_unmapped_reference_alone(self) -> None:
        """A reference with no mapping entry is left unchanged."""
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/text/ch1.xhtml",
            xhtml='<?xml version="1.0"?><html><body><img src="../unknown.png"/></body></html>',
        )
        assets = AssetPlan(files={}, media_types={}, mapping={})

        result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        # No mapping, so path stays the same
        assert result[0].xhtml == chapter.xhtml

    def test_rewrites_stylesheet_link(self) -> None:
        """Stylesheet <link> href is rewritten like other references."""
        xhtml = (
            '<?xml version="1.0"?>'
            '<html><head><link rel="stylesheet" href="../css/style.css"/></head></html>'
        )
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/text/ch1.xhtml",
            xhtml=xhtml,
        )
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={(0, "OEBPS/css/style.css"): "css/style.css"},
        )

        result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        xml = result[0].xhtml.decode("utf-8")
        assert 'href="css/style.css"' in xml

    def test_scopes_mapping_by_book_index(self) -> None:
        """Each chapter uses its own book's mapping entries."""
        ch1 = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/ch1.xhtml",
            xhtml='<?xml version="1.0"?><html><body><img src="pic.png"/></body></html>',
            book_index=0,
        )
        ch2 = _make_chapter(
            label="Chapter 2",
            filename="chapter_002_two.xhtml",
            item_id="chapter_002_two",
            source_href="ch2.xhtml",
            xhtml='<?xml version="1.0"?><html><body><img src="pic.png"/></body></html>',
            book_index=1,
        )
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={
                (0, "OEBPS/pic.png"): "pic_from_book0.png",
                (1, "pic.png"): "pic_from_book1.png",
            },
        )

        result = rewrite_chapter_links([ch1, ch2], assets)

        assert len(result) == 2
        xml1 = result[0].xhtml.decode("utf-8")
        xml2 = result[1].xhtml.decode("utf-8")
        assert 'src="pic_from_book0.png"' in xml1
        assert 'src="pic_from_book1.png"' in xml2

    def test_unchanged_chapter_keeps_original_bytes(self) -> None:
        """A chapter with no rewritten references returns the same bytes."""
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/ch1.xhtml",
            xhtml='<?xml version="1.0"?><html><body><p>Plain text</p></body></html>',
        )
        assets = AssetPlan(files={}, media_types={}, mapping={})

        result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        assert result[0].xhtml == chapter.xhtml

    def test_malformed_chapter_is_kept_and_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        """Malformed chapter returned unchanged and logged at WARNING."""
        chapter = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/ch1.xhtml",
            xhtml="<html><body><p>unclosed",
        )
        assets = AssetPlan(files={}, media_types={}, mapping={})

        with caplog.at_level("WARNING"):
            result = rewrite_chapter_links([chapter], assets)

        assert len(result) == 1
        assert result[0].xhtml == chapter.xhtml
        assert "could not rewrite references" in caplog.text

    def test_logs_info_summary(self, caplog: pytest.LogCaptureFixture) -> None:
        """Function logs info summary of rewritten chapters."""
        xhtml1 = '<?xml version="1.0"?><html><body><img src="../images/divider.png"/></body></html>'
        xhtml2 = '<?xml version="1.0"?><html><body>Plain</body></html>'
        ch1 = _make_chapter(
            label="Chapter 1",
            filename="chapter_001_one.xhtml",
            item_id="chapter_001_one",
            source_href="OEBPS/text/ch1.xhtml",
            xhtml=xhtml1,
        )
        ch2 = _make_chapter(
            label="Chapter 2",
            filename="chapter_002_two.xhtml",
            item_id="chapter_002_two",
            source_href="OEBPS/ch2.xhtml",
            xhtml=xhtml2,
        )
        assets = AssetPlan(
            files={},
            media_types={},
            mapping={(0, "OEBPS/images/divider.png"): "images/divider.png"},
        )

        with caplog.at_level("INFO"):
            rewrite_chapter_links([ch1, ch2], assets)

        assert "Merge rewrote references in" in caplog.text


class TestMergedChapterStylesheetLinks:
    """Test that stylesheet links in merged chapters resolve correctly (SH-11)."""

    def test_merged_chapter_stylesheet_links_resolve(
        self, build_epub: Callable[..., Path], tmp_path: Path
    ) -> None:
        """After merging, stylesheet links in chapters exist in archive (SH-11)."""
        # Create two simple books with stylesheet references
        target = build_epub(
            [("Chapter 1", "https://example.com/s/1")],
            doc_title="Book A",
            filename="a.epub",
        )
        source = build_epub(
            [("Chapter 2", "https://example.com/s/2")],
            doc_title="Book B",
            filename="b.epub",
        )

        # Merge them (this will call rewrite_chapter_links internally)
        from epub_merge.merge import merge_epubs

        merge_epubs(target, [source])

        # Open the merged result and check every chapter's stylesheet links exist
        doc = EpubDocument.open(target)
        archive = EpubArchive.open(target)
        import posixpath

        for spine_item in doc.opf.spine():
            # Get the manifest item to find the chapter href
            manifest_item = doc.opf.item_by_id(spine_item.idref)
            if manifest_item is None:
                continue
            # Use the chapter method which handles manifest-relative path resolution
            chapter = doc.chapter(manifest_item.href)

            for link_href in chapter.stylesheet_hrefs():
                # Stylesheet hrefs in the chapter are relative to the chapter's location.
                # The chapter is at manifest_item.href (OPF-relative).
                # Resolve the stylesheet href relative to the chapter.
                chapter_dir = posixpath.dirname(manifest_item.href)
                resolved_href = (
                    posixpath.normpath(posixpath.join(chapter_dir, link_href))
                    if chapter_dir
                    else posixpath.normpath(link_href)
                )

                # Resolve to archive path
                archive_path = (
                    posixpath.join(doc.opf_dir, resolved_href) if doc.opf_dir else resolved_href
                )
                msg = f"Stylesheet {archive_path} not found in merged EPUB"
                assert archive.has(archive_path), msg
