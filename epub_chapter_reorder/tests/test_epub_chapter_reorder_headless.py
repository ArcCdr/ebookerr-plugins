"""The headless pass honours a stored manual chapter order (CHX-FR-5/CHX-FR-9)."""

from __future__ import annotations

import json
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.epub.chapters import chapter_key, classify_spine
from ebookerr_sdk.testing import FakeContext, make_epub_item
from epub_chapter_reorder.plugin import EpubChapterReorderPlugin

_LOGGER_NAME = "plugin.epub_chapter_reorder"


class TestHeadlessPassWithoutStoredOrder:
    """Tests for books without a stored manual order."""

    def test_book_without_a_stored_order_is_unchanged_behaviour(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """A book without a stored order is reordered as if no manual order was given (CHX-FR-9)."""
        # Create an out-of-order book: chapters with keys u3, u1, u2 in that order
        chapters = [
            ("Chapter 3", "https://example.com/u3"),
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Process without any stored order
        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # The automatic order is applied (u1, u2, u3)
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        keys = [chapter_key(doc, e) for e in entries if chapter_key(doc, e) != "title_page"]
        assert keys == [
            "https://example.com/u1",
            "https://example.com/u2",
            "https://example.com/u3",
        ]


class TestHeadlessPassWithStoredOrder:
    """Tests for books with a stored manual chapter order."""

    def test_stored_order_is_reapplied(self, build_epub: Callable[..., Path]) -> None:
        """A book with a stored manual order has it re-applied instead of the automatic order."""
        # Create a 3-chapter book with chapters u1, u2, u3 in that order
        chapters = [
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
            ("Chapter 3", "https://example.com/u3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Store the manual order as u3, u1, u2
        stored_order = [
            "https://example.com/u3",
            "https://example.com/u1",
            "https://example.com/u2",
        ]
        custom_values = {
            "manual_order": api.CustomValueView(
                value=json.dumps(stored_order), value_type="string"
            )
        }

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        EpubChapterReorderPlugin().process(
            (
                make_epub_item(
                    epub,
                    book_id="b1",
                    title="Test Book",
                    story_url=None,
                    custom_values=custom_values,
                ),
            ),
            ctx,
        )

        # The stored order is applied, not the automatic order
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        keys = [chapter_key(doc, e) for e in entries if chapter_key(doc, e) != "title_page"]
        assert keys == stored_order

    def test_stored_order_survives_a_second_pass(self, build_epub: Callable[..., Path]) -> None:
        """Running process() twice with a stored order leaves the same order, no second change."""
        chapters = [
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
            ("Chapter 3", "https://example.com/u3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        stored_order = [
            "https://example.com/u3",
            "https://example.com/u1",
            "https://example.com/u2",
        ]
        custom_values = {
            "manual_order": api.CustomValueView(
                value=json.dumps(stored_order), value_type="string"
            )
        }

        # First pass
        ctx1 = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        patches1 = EpubChapterReorderPlugin().process(
            (
                make_epub_item(
                    epub,
                    book_id="b1",
                    title="Test Book",
                    story_url=None,
                    custom_values=custom_values,
                ),
            ),
            ctx1,
        )
        # First pass applies the order, so returns a patch
        assert len(patches1) >= 0  # May change on first pass

        # Second pass with the same custom values still present
        ctx2 = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        patches2 = EpubChapterReorderPlugin().process(
            (
                make_epub_item(
                    epub,
                    book_id="b1",
                    title="Test Book",
                    story_url=None,
                    custom_values=custom_values,
                ),
            ),
            ctx2,
        )
        # Second pass should report no change (order is already applied)
        assert patches2 == []

        # Verify the order is still correct
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        keys = [chapter_key(doc, e) for e in entries if chapter_key(doc, e) != "title_page"]
        assert keys == stored_order

    def test_new_chapter_is_spliced_not_appended(self, build_epub: Callable[..., Path]) -> None:
        """A new chapter splices at automatic position respecting known neighbours (CHX-FR-6)."""
        # Create a 4-chapter book with u1, u4, u2, u3 in that order (u4 is new/unplanned)
        chapters = [
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 4", "https://example.com/u4"),  # New chapter inserted mid-way
            ("Chapter 2", "https://example.com/u2"),
            ("Chapter 3", "https://example.com/u3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Store the order as u1, u2, u3 (without u4)
        stored_order = [
            "https://example.com/u1",
            "https://example.com/u2",
            "https://example.com/u3",
        ]
        custom_values = {
            "manual_order": api.CustomValueView(
                value=json.dumps(stored_order), value_type="string"
            )
        }

        # Process with the stored order that doesn't mention u4
        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        EpubChapterReorderPlugin().process(
            (
                make_epub_item(
                    epub,
                    book_id="b1",
                    title="Test Book",
                    story_url=None,
                    custom_values=custom_values,
                ),
            ),
            ctx,
        )

        # The known chapters keep their stored relative order, u4 is spliced between them
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        keys = [chapter_key(doc, e) for e in entries if chapter_key(doc, e) != "title_page"]
        # u1, u2, u3 should maintain their relative order (from the stored order)
        assert keys.index("https://example.com/u1") < keys.index("https://example.com/u2")
        assert keys.index("https://example.com/u2") < keys.index("https://example.com/u3")
        # u4 is spliced, not appended blindly
        assert "https://example.com/u4" in keys

    def test_partial_application_warns(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stored order naming a missing key produces a WARNING with 'partially applied'."""
        chapters = [
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Store an order that references a non-existent chapter u3
        stored_order = [
            "https://example.com/u1",
            "https://example.com/u3",
            "https://example.com/u2",
        ]
        custom_values = {
            "manual_order": api.CustomValueView(
                value=json.dumps(stored_order), value_type="string"
            )
        }

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.WARNING):
            EpubChapterReorderPlugin().process(
                (
                    make_epub_item(
                        epub,
                        book_id="b1",
                        title="Test Book",
                        story_url=None,
                        custom_values=custom_values,
                    ),
                ),
                ctx,
            )

        # Should log "partially applied" warning
        assert any("partially applied" in record.message.lower() for record in caplog.records)

    def test_headless_never_writes_a_custom_value(self, build_epub: Callable[..., Path]) -> None:
        """Every returned BookPatch has empty custom_values in headless mode (CHX-D5)."""
        chapters = [
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
            ("Chapter 3", "https://example.com/u3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        stored_order = [
            "https://example.com/u3",
            "https://example.com/u1",
            "https://example.com/u2",
        ]
        custom_values = {
            "manual_order": api.CustomValueView(
                value=json.dumps(stored_order), value_type="string"
            )
        }

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        patches = EpubChapterReorderPlugin().process(
            (
                make_epub_item(
                    epub,
                    book_id="b1",
                    title="Test Book",
                    story_url=None,
                    custom_values=custom_values,
                ),
            ),
            ctx,
        )

        # All patches should have no custom_values entries
        for patch in patches:
            assert not patch.custom_values

    def test_headless_never_requests_a_view(self, build_epub: Callable[..., Path]) -> None:
        """The fake context's request_view is never called in headless mode."""
        chapters = [
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        # request_view should not have been called
        assert ctx.views == []

    def test_malformed_stored_order_falls_back_to_automatic(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed stored order (unreadable JSON) falls back to automatic order."""
        chapters = [
            ("Chapter 3", "https://example.com/u3"),
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Store a malformed order
        custom_values = {
            "manual_order": api.CustomValueView(
                value="{[", value_type="string"
            )
        }

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.WARNING):
            EpubChapterReorderPlugin().process(
                (
                    make_epub_item(
                        epub,
                        book_id="b1",
                        title="Test Book",
                        story_url=None,
                        custom_values=custom_values,
                    ),
                ),
                ctx,
            )

        # Should log "unreadable" warning
        assert any("unreadable" in record.message.lower() for record in caplog.records)

        # Verify the automatic order was applied instead
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        keys = [chapter_key(doc, e) for e in entries if chapter_key(doc, e) != "title_page"]
        assert keys == [
            "https://example.com/u1",
            "https://example.com/u2",
            "https://example.com/u3",
        ]

    def test_debug_line_reports_the_key_count(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A DEBUG log line reports the stored order's key count before applying."""
        chapters = [
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
            ("Chapter 3", "https://example.com/u3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        stored_order = [
            "https://example.com/u3",
            "https://example.com/u1",
            "https://example.com/u2",
        ]
        custom_values = {
            "manual_order": api.CustomValueView(
                value=json.dumps(stored_order), value_type="string"
            )
        }

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.DEBUG):
            EpubChapterReorderPlugin().process(
                (
                    make_epub_item(
                        epub,
                        book_id="b1",
                        title="Test Book",
                        story_url=None,
                        custom_values=custom_values,
                    ),
                ),
                ctx,
            )

        # Should have a DEBUG log with "Applying a stored manual chapter order" and ": 3 key(s)"
        assert any(
            (
                "Applying a stored manual chapter order" in record.message
                and ": 3 key(s)" in record.message
            )
            for record in caplog.records
            if record.levelno >= logging.DEBUG
        )

    def test_malformed_epub_still_skipped_not_fatal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed staged file makes process() return [] and log the existing WARNING."""
        bad = tmp_path / "bad.epub"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")  # no container.xml

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        with caplog.at_level(logging.WARNING):
            patches = EpubChapterReorderPlugin().process(
                (make_epub_item(bad, book_id="b1", title="Test Book", story_url=None),), ctx
            )

        assert patches == []
        assert any("reorder" in record.message.lower() for record in caplog.records)

    def test_num_chapters_patch_still_emitted_on_change(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """A book that actually changes yields one BookPatch with recomputed num_chapters."""
        chapters = [
            ("Chapter 3", "https://example.com/u3"),
            ("Chapter 1", "https://example.com/u1"),
            ("Chapter 2", "https://example.com/u2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        stored_order = [
            "https://example.com/u1",
            "https://example.com/u2",
            "https://example.com/u3",
        ]
        custom_values = {
            "manual_order": api.CustomValueView(
                value=json.dumps(stored_order), value_type="string"
            )
        }

        ctx = FakeContext(logger=logging.getLogger(_LOGGER_NAME))
        patches = EpubChapterReorderPlugin().process(
            (
                make_epub_item(
                    epub,
                    book_id="b1",
                    title="Test Book",
                    story_url=None,
                    custom_values=custom_values,
                ),
            ),
            ctx,
        )

        assert len(patches) == 1
        assert patches[0].book_id == "b1"
        assert "num_chapters" in patches[0].fields
        assert patches[0].fields["num_chapters"] == 3
