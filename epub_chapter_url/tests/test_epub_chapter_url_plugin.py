"""Tests for EpubChapterUrlPlugin."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.testing import Cancelled, FakeContext
from epub_chapter_url.plugin import EpubChapterUrlPlugin

_L = "https://www.literotica.com/s/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_book_view(
    book_id: str = "b1",
    story_url: str | None = None,
    chapters: tuple[api.ChapterLink, ...] = (),
) -> api.BookView:
    return api.BookView(
        book_id=book_id,
        title="Test Book",
        author="Test Author",
        story_url=story_url,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=chapters,
    )


def _make_item(
    epub_path: Path,
    book_id: str = "b1",
    story_url: str | None = None,
    chapters: tuple[api.ChapterLink, ...] = (),
) -> api.EpubItem:
    return api.EpubItem(
        book=_make_book_view(book_id, story_url=story_url, chapters=chapters),
        epub_path=epub_path,
    )


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_shape(self) -> None:
        plugin = EpubChapterUrlPlugin()
        assert plugin.manifest.id == "epub_chapter_url"
        assert plugin.manifest.plugin_type == api.PluginType.EPUB
        assert plugin.manifest.headless is True
        assert plugin.manifest.events == (
            api.PluginEventType.EPUB_CREATED,
            api.PluginEventType.EPUB_MODIFIED,
        )

    def test_settings_schema_is_empty(self) -> None:
        plugin = EpubChapterUrlPlugin()
        assert plugin.settings_schema() == api.SettingsSchema()

    def test_manifest_priority_is_sixty(self) -> None:
        plugin = EpubChapterUrlPlugin()
        assert plugin.manifest.priority == 60


# ---------------------------------------------------------------------------
# process() — single chapter
# ---------------------------------------------------------------------------


class TestSingleChapter:
    def test_single_chapter_epub_is_stamped(self, build_epub: Callable[..., Path]) -> None:
        # Use empty URL so build_epub doesn't pre-fill chapterurl
        epub = build_epub([("Chapter One", "")], doc_title="Test Book")
        item = _make_item(epub, story_url="https://book/one")
        EpubChapterUrlPlugin().process((item,), FakeContext())

        doc = EpubDocument.open(epub)
        links = doc.chapter_links()
        # Stamped with the book URL; chapter_links() resolves the chapter's own title.
        assert links == [("https://book/one", "Chapter One", 1)]

    def test_collect_content_hrefs_matches_content_chapters(
        self, build_epub: Callable[..., Path]
    ) -> None:
        epub = build_epub([("Ch. 1", ""), ("Ch. 2", "")], include_title_page=True)
        doc = EpubDocument.open(epub)
        plugin = EpubChapterUrlPlugin()

        collected = plugin._collect_content_hrefs(doc)
        expected = [item.href for item in doc.content_chapters()]

        assert collected == expected == ["OEBPS/file0001.xhtml", "OEBPS/file0002.xhtml"]

    def test_patch_declares_chapter_link(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "")], doc_title="Test Book")
        item = _make_item(epub, book_id="book-1", story_url="https://book/one")
        patches = EpubChapterUrlPlugin().process((item,), FakeContext())

        assert len(patches) == 1
        assert patches[0].book_id == "book-1"
        assert patches[0].chapters == (api.ChapterLink(url="https://book/one", title=None),)

    def test_already_declared_single_chapter_untouched(
        self, build_epub: Callable[..., Path]
    ) -> None:
        epub = build_epub([("Chapter One", "")], doc_title="Test Book")
        # Pre-stamp the chapter
        doc = EpubDocument.open(epub)
        # With 1 chapter: title_page (separate) and file0001 (content)
        chapter = doc.chapter("OEBPS/file0001.xhtml")
        chapter.set_meta("chapterurl", "https://book/one")
        doc.write_chapter("OEBPS/file0001.xhtml", chapter)
        doc.save()

        before = epub.read_bytes()
        item = _make_item(epub, story_url="https://book/one")
        patches = EpubChapterUrlPlugin().process((item,), FakeContext())

        assert patches == []
        assert epub.read_bytes() == before

    def test_no_patch_when_already_indexed(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "")], doc_title="Test Book")
        item = _make_item(
            epub,
            book_id="book-1",
            story_url="https://book/one",
            chapters=(api.ChapterLink(url="https://book/one"),),
        )
        patches = EpubChapterUrlPlugin().process((item,), FakeContext())

        # File should still be stamped
        doc = EpubDocument.open(epub)
        links = doc.chapter_links()
        assert links == [("https://book/one", "Chapter One", 1)]

        # But no patch returned (already in index)
        assert patches == []


# ---------------------------------------------------------------------------
# process() — multi-chapter
# ---------------------------------------------------------------------------


class TestMultiChapter:
    def test_multi_chapter_none_declared_all_stamped(self, build_epub: Callable[..., Path]) -> None:
        chapters = [
            ("Chapter One", ""),
            ("Chapter Two", ""),
            ("Chapter Three", ""),
        ]
        epub = build_epub(chapters, doc_title="Test Book")
        item = _make_item(epub, story_url="https://book/all")
        EpubChapterUrlPlugin().process((item,), FakeContext())

        # Verify all chapters were stamped
        doc = EpubDocument.open(epub)
        for entry in doc.opf.spine():
            if entry.idref in {"title_page"}:
                continue
            manifest_item = next((m for m in doc.opf.manifest() if m.id == entry.idref), None)
            if manifest_item:
                chapter = doc.chapter(manifest_item.href)
                url = chapter.meta("chapterurl")
                assert url == "https://book/all", f"Chapter {entry.idref} not stamped"

    def test_multi_chapter_partial_left_untouched(self, build_epub: Callable[..., Path]) -> None:
        chapters = [
            ("Chapter One", ""),
            ("Chapter Two", ""),
            ("Chapter Three", ""),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Pre-declare only one chapter (file0002 is the middle chapter)
        doc = EpubDocument.open(epub)
        chapter = doc.chapter("OEBPS/file0002.xhtml")
        chapter.set_meta("chapterurl", "https://x/2")
        doc.write_chapter("OEBPS/file0002.xhtml", chapter)
        doc.save()

        item = _make_item(epub, story_url="https://book/all")
        EpubChapterUrlPlugin().process((item,), FakeContext())

        doc = EpubDocument.open(epub)
        links = doc.chapter_links()
        # When one chapter declares a URL, nothing is stamped (multi-chapter rule) — only
        # the pre-declared one remains.
        assert links == [("https://x/2", "Chapter Two", 2)]


# ---------------------------------------------------------------------------
# process() — error cases and edge cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_missing_story_url_skips(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "")], doc_title="Test Book")
        before = epub.read_bytes()

        item = _make_item(epub, story_url=None)
        with caplog.at_level(logging.DEBUG):
            patches = EpubChapterUrlPlugin().process((item,), FakeContext())

        assert patches == []
        assert epub.read_bytes() == before
        assert any("no story URL" in record.message for record in caplog.records)

    def test_malformed_epub_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        bad = tmp_path / "bad.epub"
        bad.write_text("not an epub")

        item = _make_item(bad, story_url="https://book/one")
        with caplog.at_level(logging.WARNING):
            patches = EpubChapterUrlPlugin().process((item,), FakeContext())

        assert patches == []
        assert any("malformed" in record.message.lower() for record in caplog.records)

    def test_title_page_not_stamped(self, build_epub: Callable[..., Path]) -> None:
        # build_epub creates standard FanFicFare structure with title_page manifest item
        # as the title page and file000X items as content chapters
        chapters = [
            ("Chapter One", ""),
            ("Chapter Two", ""),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        item = _make_item(epub, story_url="https://book/one")
        EpubChapterUrlPlugin().process((item,), FakeContext())

        # Verify that only content chapters were stamped, not title page
        doc = EpubDocument.open(epub)

        # Check title page was NOT stamped
        title_page = next((m for m in doc.opf.manifest() if m.id == "title_page"), None)
        if title_page:
            chapter = doc.chapter(title_page.href)
            url = chapter.meta("chapterurl")
            assert not url, "Title page should not be stamped"

        # Check that content chapters were stamped
        content_count = 0
        for entry in doc.opf.spine():
            if entry.idref in {"title_page"}:
                continue
            manifest_item = next((m for m in doc.opf.manifest() if m.id == entry.idref), None)
            if manifest_item:
                chapter = doc.chapter(manifest_item.href)
                url = chapter.meta("chapterurl")
                assert url == "https://book/one", f"Content chapter {entry.idref} not stamped"
                content_count += 1

        assert content_count == 2


# ---------------------------------------------------------------------------
# process() — batch and progress
# ---------------------------------------------------------------------------


class TestBatch:
    def test_batch_processes_every_item(self, build_epub: Callable[..., Path]) -> None:
        epub1 = build_epub([("Ch1", "")], filename="book1.epub", doc_title="Book 1")
        epub2 = build_epub([("Ch1", "")], filename="book2.epub", doc_title="Book 2")
        epub3 = build_epub([("Ch1", "")], filename="book3.epub", doc_title="Book 3")

        items = (
            _make_item(epub1, book_id="book-1", story_url="https://book/1"),
            _make_item(epub2, book_id="book-2", story_url="https://book/2"),
            _make_item(epub3, book_id="book-3", story_url="https://book/3"),
        )
        patches = EpubChapterUrlPlugin().process(items, FakeContext())

        assert len(patches) == 3
        assert {p.book_id for p in patches} == {"book-1", "book-2", "book-3"}

    def test_cancellation_is_checked(self, build_epub: Callable[..., Path]) -> None:
        epub1 = build_epub([("Ch1", "")], filename="book1.epub", doc_title="Book 1")
        epub2 = build_epub([("Ch1", "")], filename="book2.epub", doc_title="Book 2")
        epub3 = build_epub([("Ch1", "")], filename="book3.epub", doc_title="Book 3")

        items = (
            _make_item(epub1, book_id="book-1", story_url="https://book/1"),
            _make_item(epub2, book_id="book-2", story_url="https://book/2"),
            _make_item(epub3, book_id="book-3", story_url="https://book/3"),
        )

        # Cancels after the first item's check_cancelled() passes, before the second's.
        ctx = FakeContext(cancel_after=1)

        with pytest.raises(Cancelled):
            EpubChapterUrlPlugin().process(items, ctx)

        # The first item was processed before cancellation; the rest were not.
        assert len(EpubDocument.open(epub1).chapter_links()) > 0
        assert len(EpubDocument.open(epub2).chapter_links()) == 0
        assert len(EpubDocument.open(epub3).chapter_links()) == 0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestLogging:
    def test_logs_stamp_count(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        chapters = [
            ("Chapter One", ""),
            ("Chapter Two", ""),
            ("Chapter Three", ""),
        ]
        epub = build_epub(chapters, doc_title="Test Book")
        item = _make_item(epub, book_id="book-42", story_url="https://book/all")

        with caplog.at_level(logging.INFO):
            patches = EpubChapterUrlPlugin().process((item,), FakeContext())

        # Should get 1 patch (URL not already indexed)
        assert len(patches) == 1

        assert any(
            "Chapter URL stamped on 3 chapter(s) of" in record.message
            and "book-42" in record.message
            for record in caplog.records
        )

    def test_logs_debug_when_already_declared(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "")], doc_title="Test Book")

        # Pre-declare the chapter
        doc = EpubDocument.open(epub)
        chapter = doc.chapter("OEBPS/file0001.xhtml")  # With 1 chapter: title_page + file0001
        chapter.set_meta("chapterurl", "https://book/one")
        doc.write_chapter("OEBPS/file0001.xhtml", chapter)
        doc.save()

        item = _make_item(epub, book_id="book-42", story_url="https://book/one")
        with caplog.at_level(logging.DEBUG):
            EpubChapterUrlPlugin().process((item,), FakeContext())

        assert any(
            "Chapter URLs already declared for book_id=" in record.message
            and "book-42" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_idempotent_second_run(self, build_epub: Callable[..., Path]) -> None:
        chapters = [
            ("Chapter One", ""),
            ("Chapter Two", ""),
            ("Chapter Three", ""),
        ]
        epub = build_epub(chapters, doc_title="Test Book")
        item = _make_item(epub, book_id="book-1", story_url="https://book/all")

        # First run
        EpubChapterUrlPlugin().process((item,), FakeContext())
        after_first = epub.read_bytes()

        # Second run
        patches2 = EpubChapterUrlPlugin().process((item,), FakeContext())
        after_second = epub.read_bytes()

        # Files should be identical
        assert after_first == after_second
        # Second run should return no patches
        assert patches2 == []
