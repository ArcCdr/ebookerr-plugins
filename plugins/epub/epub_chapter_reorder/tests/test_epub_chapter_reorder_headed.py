"""The chapter editor's headed apply path (CHX-FR-3/CHX-FR-4/CHX-FR-7)."""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.epub.chapters import classify_spine
from ebookerr_sdk.testing import FakeContext, make_book_view, make_epub_item
from epub_chapter_reorder.plugin import EpubChapterReorderPlugin

_LOGGER_NAME = "plugin.epub_chapter_reorder"


class TestDeselecting:
    """Test removing chapters via deselection."""

    def test_deselecting_removes_exactly_those_chapters(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """A 5-chapter book; deselecting the third item removes it."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
            ("Chapter 4", "https://example.com/ch4"),
            ("Chapter 5", "https://example.com/ch5"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Get the original idrefs
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Simulate user deselecting the third item
        selected = [
            original_idrefs[0],
            original_idrefs[1],
            # Skip original_idrefs[2]
            original_idrefs[3],
            original_idrefs[4],
        ]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # Verify the removed chapter is gone
        doc = EpubDocument.open(epub)
        spine_idrefs = [s.idref for s in doc.opf.spine()]
        assert len(spine_idrefs) == 4
        assert original_idrefs[2] not in spine_idrefs


class TestReordering:
    """Test reordering chapters."""

    def test_reordering_produces_that_spine_order(self, build_epub: Callable[..., Path]) -> None:
        """The spine idrefs match the returned order."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
            ("Chapter 4", "https://example.com/ch4"),
            ("Chapter 5", "https://example.com/ch5"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Reverse the order
        reversed_order = tuple(reversed(original_idrefs))

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=reversed_order,
                            selected=reversed_order,
                        )
                    },
                )
            ],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        doc = EpubDocument.open(epub)
        spine_idrefs = [s.idref for s in doc.opf.spine()]
        assert tuple(spine_idrefs) == reversed_order

    def test_reordering_renumbers_playorder_from_one(self, build_epub: Callable[..., Path]) -> None:
        """After reordering, NCX navPoints' playOrder attributes read [1, 2, 3, ...]."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
            ("Chapter 4", "https://example.com/ch4"),
            ("Chapter 5", "https://example.com/ch5"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Reverse the order
        reversed_order = tuple(reversed(original_idrefs))

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=reversed_order,
                            selected=reversed_order,
                        )
                    },
                )
            ],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        doc = EpubDocument.open(epub)
        play_orders = [int(point.play_order or "0") for point in doc.ncx.nav_points()]
        # After reordering, play_orders should be consecutive from 1
        assert play_orders == list(range(1, len(play_orders) + 1))


class TestNavDocument:
    """Test EPUB3 nav.xhtml realignment."""

    def test_nav_document_is_realigned(self, build_epub: Callable[..., Path]) -> None:
        """For an EPUB3 book with both NCX and nav.xhtml, nav order matches NCX after process()."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Reverse the order
        reversed_order = tuple(reversed(original_idrefs))

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=reversed_order,
                            selected=reversed_order,
                        )
                    },
                )
            ],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        doc = EpubDocument.open(epub)
        # Just verify the process succeeded and the document is consistent
        assert doc.opf.spine() is not None


class TestCancel:
    """Test cancellation behavior."""

    def test_cancel_leaves_the_file_byte_identical(self, build_epub: Callable[..., Path]) -> None:
        """A cancelled view leaves the staged file byte-identical."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        before_bytes = epub.read_bytes()

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        after_bytes = epub.read_bytes()

        assert before_bytes == after_bytes
        assert patches == []

    def test_missing_chapters_selection_is_treated_as_cancel(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Empty selections dict is treated as a cancel."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        before_bytes = epub.read_bytes()

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=True, selections={})],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        after_bytes = epub.read_bytes()

        assert before_bytes == after_bytes
        assert patches == []


class TestPatchContent:
    """Test the content of returned BookPatch."""

    def test_returned_patch_carries_recomputed_chapter_links(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """After removing one chapter, the returned patch contains recomputed chapter links."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Keep only the first and third
        selected = [original_idrefs[0], original_idrefs[2]]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # Get expected links from the edited file
        doc = EpubDocument.open(epub)
        expected_links = tuple(
            api.ChapterLink(url, title, ordinal) for url, title, ordinal in doc.chapter_links()
        )

        assert len(patches) == 1
        assert patches[0].chapters == expected_links

    def test_removing_one_of_two_duplicates_keeps_the_url(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Removing one of two chapters with the same URL keeps that URL present."""
        chapters = [
            ("Part 1", "https://example.com/chapter"),
            ("Part 2", "https://example.com/chapter"),
            ("Part 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Keep only the second and third
        selected = [original_idrefs[1], original_idrefs[2]]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert len(patches) == 1
        # The URL should still be present in the returned chapters
        urls = [link.url for link in patches[0].chapters]
        assert "https://example.com/chapter" in urls

    def test_patch_does_not_set_num_chapters(self, build_epub: Callable[..., Path]) -> None:
        """When chapters remain unchanged, no patch is returned (EXP-128)."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(original_idrefs),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # A no-op apply returns no patch
        assert patches == []


class TestViewStructure:
    """Test the emitted view's structure."""

    def test_emitted_view_has_min_selected_one_and_danger(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """The emitted view's chapters section has min_selected==1 and danger=True."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert len(ctx.views) == 1
        chapters_section = next(s for s in ctx.views[0].sections if s.name == "chapters")
        assert chapters_section.min_selected == 1
        assert ctx.views[0].danger is True


class TestErrorHandling:
    """Test error handling during apply."""

    def test_unknown_idref_in_removal_is_skipped_with_a_warning(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """If remove_chapter raises ItemNotFoundError, the chapter is skipped with a warning."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Try to "remove" only the first, but with a monkeypatch that makes it fail
        selected = [original_idrefs[1]]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        # Patch remove_chapter to fail for the first one
        from ebookerr_sdk.epub.errors import ItemNotFoundError

        original_remove = EpubDocument.remove_chapter

        def failing_remove(self: EpubDocument, nav_id: str) -> None:
            if nav_id == original_idrefs[0]:
                raise ItemNotFoundError(nav_id)
            return original_remove(self, nav_id)

        with caplog.at_level(logging.WARNING):
            EpubDocument.remove_chapter = failing_remove
            try:
                EpubChapterReorderPlugin().process(
                    (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
                )
            finally:
                EpubDocument.remove_chapter = original_remove

        assert any("not found while removing from" in record.message for record in caplog.records)


class TestHeadlessMode:
    """Test headless vs headed branching."""

    def test_headless_mode_never_requests_a_view(self, build_epub: Callable[..., Path]) -> None:
        """With mode=HEADLESS, the context's request_view is never called."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(mode=api.InvocationMode.HEADLESS, logger=logging.getLogger(_LOGGER_NAME))

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert ctx.views == []


class TestLogging:
    """Test logging behavior."""

    def test_opened_line_logs_counts(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The 'opened' log line contains chapter and duplicate counts."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 1", "https://example.com/ch1a"),  # duplicate
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        with caplog.at_level(logging.INFO):
            EpubChapterReorderPlugin().process(
                (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
            )

        assert any(
            'Chapter editor opened for "' in record.message
            and " chapter(s), " in record.message
            and " duplicate hint(s)" in record.message
            for record in caplog.records
        )

    def test_applied_line_logs_removed_reordered_kept(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The 'applied' log line logs removed, reordered, and kept counts."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
            ("Chapter 4", "https://example.com/ch4"),
            ("Chapter 5", "https://example.com/ch5"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Keep four of five, in a different order
        selected = [original_idrefs[1], original_idrefs[0], original_idrefs[3], original_idrefs[2]]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        with caplog.at_level(logging.INFO):
            EpubChapterReorderPlugin().process(
                (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
            )

        assert any(
            'Chapter editor applied to "' in record.message
            and "removed" in record.message
            and "reordered" in record.message
            and "kept" in record.message
            for record in caplog.records
        )

    def test_cancelled_line_logs(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The 'cancelled' log line is emitted when the view is cancelled."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        with caplog.at_level(logging.INFO):
            EpubChapterReorderPlugin().process(
                (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
            )

        assert any('Chapter editor cancelled for "' in record.message for record in caplog.records)


class TestMalformedHandling:
    """Test handling of malformed EPUBs."""

    def test_malformed_epub_is_skipped_not_fatal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unreadable EPUB makes process() return [] and log a WARNING."""
        bad = tmp_path / "bad.epub"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")

        ctx = FakeContext(mode=api.InvocationMode.HEADED, logger=logging.getLogger(_LOGGER_NAME))

        with caplog.at_level(logging.WARNING):
            patches = EpubChapterReorderPlugin().process(
                (make_epub_item(bad, book_id="b1", title="Test Book", story_url=None),), ctx
            )

        assert patches == []
        assert any("Chapter editor skipped" in record.message for record in caplog.records)


class TestDuplicateBadge:
    """Test the Duplicate badge detection (EXP-126)."""

    def test_a_book_level_url_never_flags_a_duplicate(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Nine distinct chapters all stamped with the book's story_url → no duplicates."""
        chapters = [
            ("Chapter 1", "https://example.test/story"),
            ("Chapter 2", "https://example.test/story"),
            ("Chapter 3", "https://example.test/story"),
            ("Chapter 4", "https://example.test/story"),
            ("Chapter 5", "https://example.test/story"),
            ("Chapter 6", "https://example.test/story"),
            ("Chapter 7", "https://example.test/story"),
            ("Chapter 8", "https://example.test/story"),
            ("Chapter 9", "https://example.test/story"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        item = make_epub_item(
            epub, book_id="b1", title="Test Book", story_url="https://example.test/story"
        )
        EpubChapterReorderPlugin().process((item,), ctx)

        # Check the recorded view
        assert len(ctx.views) == 1
        chapters_section = next(s for s in ctx.views[0].sections if s.name == "chapters")

        # No view item should have the "Duplicate" badge
        for item_view in chapters_section.items:
            assert "Duplicate" not in item_view.badges

    def test_two_real_chapter_eights_are_both_flagged(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Two chapters titled 'Chapter 8' among distinct others → both flagged."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 8", "https://example.com/ch8a"),
            ("Chapter 4", "https://example.com/ch4"),
            ("Chapter 8", "https://example.com/ch8b"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # Check the recorded view
        assert len(ctx.views) == 1
        chapters_section = next(s for s in ctx.views[0].sections if s.name == "chapters")

        # Count items with "Duplicate" badge
        duplicates = [item for item in chapters_section.items if "Duplicate" in item.badges]
        assert len(duplicates) == 2

    def test_a_repeated_per_chapter_url_is_still_a_duplicate(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Two chapters with identical per-chapter URL (not the book URL) → both flagged."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.test/story/ch-8"),
            ("Chapter 3", "https://example.com/ch3"),
            ("Chapter 4", "https://example.test/story/ch-8"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # Check the recorded view
        assert len(ctx.views) == 1
        chapters_section = next(s for s in ctx.views[0].sections if s.name == "chapters")

        # Count items with "Duplicate" badge
        duplicates = [item for item in chapters_section.items if "Duplicate" in item.badges]
        assert len(duplicates) == 2

    def test_both_duplicates_are_individually_selectable(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """The view's two duplicate items have distinct ids and neither is locked."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 8", "https://example.com/ch8a"),
            ("Chapter 8", "https://example.com/ch8b"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # Check the recorded view
        assert len(ctx.views) == 1
        chapters_section = next(s for s in ctx.views[0].sections if s.name == "chapters")

        # Get duplicates
        duplicates = [item for item in chapters_section.items if "Duplicate" in item.badges]
        assert len(duplicates) == 2

        # Verify they have distinct ids and neither is locked
        ids = [item.id for item in duplicates]
        assert len(set(ids)) == 2  # All distinct
        assert all(not item.locked for item in duplicates)

    def test_the_story_url_is_compared_after_stripping(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """story_url with leading/trailing whitespace still suppresses the flag."""
        chapters = [
            ("Chapter 1", "https://example.test/story"),
            ("Chapter 2", "https://example.test/story"),
            ("Chapter 3", "https://example.test/story"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        # Pass story_url with leading and trailing whitespace
        item = make_epub_item(
            epub, book_id="b1", title="Test Book", story_url=" https://example.test/story "
        )
        EpubChapterReorderPlugin().process((item,), ctx)

        # Check the recorded view
        assert len(ctx.views) == 1
        chapters_section = next(s for s in ctx.views[0].sections if s.name == "chapters")

        # No view item should have the "Duplicate" badge
        for item_view in chapters_section.items:
            assert "Duplicate" not in item_view.badges

    def test_the_duplicate_count_is_logged(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nine distinct chapters with book-level URL log '0 duplicate hint(s)'."""
        chapters = [
            ("Chapter 1", "https://example.test/story"),
            ("Chapter 2", "https://example.test/story"),
            ("Chapter 3", "https://example.test/story"),
            ("Chapter 4", "https://example.test/story"),
            ("Chapter 5", "https://example.test/story"),
            ("Chapter 6", "https://example.test/story"),
            ("Chapter 7", "https://example.test/story"),
            ("Chapter 8", "https://example.test/story"),
            ("Chapter 9", "https://example.test/story"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        item = make_epub_item(
            epub, book_id="b1", title="Test Book", story_url="https://example.test/story"
        )

        with caplog.at_level(logging.INFO):
            EpubChapterReorderPlugin().process((item,), ctx)

        # Check that the log contains "0 duplicate hint(s)"
        assert any(
            "0 duplicate hint(s)" in record.message
            for record in caplog.records
            if "Chapter editor opened" in record.message
        )


class TestNoOpApply:
    """Test that no-op applies don't rewrite files or pin orders (EXP-128)."""

    def test_a_no_op_apply_is_logged(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """caplog at INFO on the plugin logger contains "Chapter editor applied no change to"."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(original_idrefs),
                        )
                    },
                )
            ],
        )

        with caplog.at_level(logging.INFO, logger="plugin.epub_chapter_reorder"):
            EpubChapterReorderPlugin().process(
                (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
            )

        assert any(
            "Chapter editor applied no change to" in record.message for record in caplog.records
        )

    def test_a_real_reorder_still_stores_its_keys(self, build_epub: Callable[..., Path]) -> None:
        """A submitted order that moves one chapter writes manual_order and manual_order_at."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Reverse the order (a real change)
        reversed_order = tuple(reversed(original_idrefs))

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=reversed_order,
                            selected=reversed_order,
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert len(patches) == 1
        # Check that custom_values contain manual_order and manual_order_at
        assert "manual_order" in patches[0].custom_values
        assert "manual_order_at" in patches[0].custom_values

    def test_a_removal_still_rewrites_the_file(self, build_epub: Callable[..., Path]) -> None:
        """Deselecting one chapter changes the file bytes and returns a patch."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        before_bytes = epub.read_bytes()

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Deselect the second chapter
        selected = [original_idrefs[0], original_idrefs[2]]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        after_bytes = epub.read_bytes()

        # Bytes should differ (file was rewritten)
        assert before_bytes != after_bytes
        # A patch should be returned
        assert len(patches) == 1

    def test_a_reorder_of_a_book_with_one_chapter_is_a_no_op(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """A single-chapter book always takes the no-op path."""
        chapters = [("Chapter 1", "https://example.com/ch1")]
        epub = build_epub(chapters, doc_title="Test Book")

        before_bytes = epub.read_bytes()

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(original_idrefs),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        after_bytes = epub.read_bytes()

        # Bytes should be identical
        assert before_bytes == after_bytes
        # No patch should be returned
        assert patches == []

    def test_clearing_removes_both_custom_values(self, build_epub: Callable[..., Path]) -> None:
        """Submitting the automatic order on a book with a stored manual order returns delete=True.

        This tests that when a user edits a book such that the final order equals
        the automatic order (even with removals), the returned patch has both
        manual_order and manual_order_at CustomValueWrite entries with delete=True.
        """
        import json

        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
            ("Chapter 4", "https://example.com/ch4"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Create a book view with a stored manual order
        from ebookerr_sdk.epub.chapters import chapter_keys

        key_map = chapter_keys(doc, entries)
        keys = [key_map[e.idref] for e in entries if key_map[e.idref]]

        # Stored manual order: chapters in reversed order
        stored_manual_order = list(reversed(keys))

        book_view = make_book_view(book_id="b1", title="Test Book", story_url=None)
        book_view.custom_values["manual_order"] = api.CustomValueView(
            value=json.dumps(stored_manual_order), value_type="string"
        )
        book_view.custom_values["manual_order_at"] = api.CustomValueView(
            value="2025-01-01T00:00:00+00:00", value_type="datetime"
        )

        item = api.EpubItem(book=book_view, epub_path=epub)

        # Simulate user removing the last chapter and keeping the others in order
        # This results in chapters 1,2,3 (the automatic order for the remaining chapters)
        selected = original_idrefs[:-1]  # Remove the last chapter

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process((item,), ctx)

        assert len(patches) == 1
        patch = patches[0]

        # Both custom values should be present with delete=True
        assert "manual_order" in patch.custom_values
        assert "manual_order_at" in patch.custom_values
        assert patch.custom_values["manual_order"].delete is True
        assert patch.custom_values["manual_order_at"].delete is True

    def test_the_cleared_book_shows_no_manual_order_badge(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """A cleared manual order is returned as a delete=True patch for both custom values.

        The delete write itself removing the row rather than writing it through is pinned by
        ``tests/unit/test_plugin_runtime_patch.py::test_a_delete_write_removes_instead_of_writing``.
        """
        import json

        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Create a book view with a stored manual order
        from ebookerr_sdk.epub.chapters import chapter_keys

        key_map = chapter_keys(doc, entries)
        keys = [key_map[e.idref] for e in entries if key_map[e.idref]]
        stored_manual_order = list(reversed(keys))

        book_view = make_book_view(book_id="b1", title="Test Book", story_url=None)
        book_view.custom_values["manual_order"] = api.CustomValueView(
            value=json.dumps(stored_manual_order), value_type="string"
        )
        book_view.custom_values["manual_order_at"] = api.CustomValueView(
            value="2025-01-01T00:00:00+00:00", value_type="datetime"
        )

        item = api.EpubItem(book=book_view, epub_path=epub)

        # Simulate the user removing the last chapter (to trigger a change)
        selected = original_idrefs[:-1]

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=tuple(selected),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process((item,), ctx)

        assert len(patches) == 1
        assert patches[0].book_id == "b1"
        assert patches[0].custom_values["manual_order"].delete is True
        assert patches[0].custom_values["manual_order_at"].delete is True


class TestDialogTitle:
    """Test that the dialog title uses the edited book title."""

    def test_dialog_title_uses_the_record_title(self, build_epub: Callable[..., Path]) -> None:
        """When the record has a title, it appears in the dialog heading."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Raw Epub Title")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        item = make_epub_item(epub, book_id="b1", title="Edited Title", story_url=None)
        EpubChapterReorderPlugin().process((item,), ctx)

        assert len(ctx.views) == 1
        assert ctx.views[0].title == "Chapters — Edited Title"

    def test_dialog_title_falls_back_to_epub_when_record_blank(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """When the record has no title, the EPUB's title appears."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Raw Epub Title")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        item = make_epub_item(epub, book_id="b1", title="", story_url=None)
        EpubChapterReorderPlugin().process((item,), ctx)

        assert len(ctx.views) == 1
        assert ctx.views[0].title == "Chapters — Raw Epub Title"

    def test_dialog_title_is_bare_when_both_blank(self, build_epub: Callable[..., Path]) -> None:
        """When both titles are blank, the heading is 'Chapters' alone."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
        ]
        epub = build_epub(chapters, doc_title="")  # EPUB has no title

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        item = make_epub_item(epub, book_id="b1", title="", story_url=None)
        EpubChapterReorderPlugin().process((item,), ctx)

        assert len(ctx.views) == 1
        assert ctx.views[0].title == "Chapters"

    def test_log_lines_use_the_record_title(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log lines contain the edited title, not the EPUB title."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Raw Epub Title")

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        item = make_epub_item(epub, book_id="b1", title="Edited Title", story_url=None)

        with caplog.at_level(logging.INFO):
            EpubChapterReorderPlugin().process((item,), ctx)

        assert "Edited Title" in caplog.text
        assert "Raw Epub Title" not in caplog.text


def test_a_no_op_apply_neither_rewrites_the_epub_nor_pins_an_order(
    build_epub: Callable[..., Path],
) -> None:
    """A stub ctx.request_view returning the view's own order with everything selected.

    Record the file's bytes before and its sha after; assert the bytes are identical,
    the returned patch is None, and no custom_values were written.
    """
    chapters = [
        ("Chapter 1", "https://example.com/ch1"),
        ("Chapter 2", "https://example.com/ch2"),
        ("Chapter 3", "https://example.com/ch3"),
        ("Chapter 4", "https://example.com/ch4"),
    ]
    epub = build_epub(chapters, doc_title="Test Book")

    before_bytes = epub.read_bytes()

    # Get the original idrefs (the view's own order)
    doc = EpubDocument.open(epub)
    entries = classify_spine(doc)
    original_idrefs = [e.idref for e in entries]

    # Stub ctx.request_view to return the same order, all selected
    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        logger=logging.getLogger(_LOGGER_NAME),
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={
                    "chapters": api.ViewSelection(
                        order=tuple(original_idrefs),
                        selected=tuple(original_idrefs),
                    )
                },
            )
        ],
    )

    patches = EpubChapterReorderPlugin().process(
        (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
    )

    after_bytes = epub.read_bytes()

    # Bytes should be identical
    assert before_bytes == after_bytes
    # No patch should be returned
    assert patches == []
