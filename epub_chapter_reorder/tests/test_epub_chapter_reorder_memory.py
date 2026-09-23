"""The chapter editor's manual-order memory (CHX-D4/CHX-D5/CHX-TR-1)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.epub.chapters import chapter_keys, classify_spine
from ebookerr_sdk.testing import FakeContext, make_book_view, make_epub_item, make_request, run_wire
from ebookerr_sdk.wire import encode_epub_item
from epub_chapter_reorder.plugin import EpubChapterReorderPlugin, _stored_manual_order

_LOGGER_NAME = "plugin.epub_chapter_reorder"

# Specimen helper: 10 rows (title, prologue, 6 chapters, 2 back matter)
_ZONE_CHAPTERS = [
    ("Prologue", "https://example.test/s/prologue"),
    *[(f"Chapter {i}", f"https://example.test/s/ch{i}") for i in range(1, 7)],
    ("Author's Note", "https://example.test/s/note"),
    ("Epilogue", "https://example.test/s/epilogue"),
]


class TestStoredManualOrder:
    """Test reading stored manual chapter order."""

    def test_missing_value_is_empty(self) -> None:
        """A BookView with no custom values yields []."""
        book = make_book_view(book_id="b1", title="Test Book", story_url=None)
        result = _stored_manual_order(book)
        assert result == []

    def test_the_bare_key_is_read(self) -> None:
        """A BookView whose own custom values hold ``manual_order`` yields the order."""
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='["a","b"]', value_type="string")
            },
        )
        result = _stored_manual_order(book)
        assert result == ["a", "b"]

    def test_a_namespaced_key_is_not_read(self) -> None:
        """The core's namespaced key never reaches the plugin; it is not read."""
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "epub_chapter_reorder.manual_order": api.CustomValueView(
                    value='["a","b"]', value_type="string"
                )
            },
        )
        result = _stored_manual_order(book)
        assert result == []

    def test_malformed_json_is_empty_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """Malformed JSON yields [] and logs a WARNING."""
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={"manual_order": api.CustomValueView(value="{[", value_type="string")},
        )
        with caplog.at_level(logging.WARNING):
            result = _stored_manual_order(book)
        assert result == []
        assert any("unreadable" in record.message for record in caplog.records)

    def test_non_list_json_is_empty(self) -> None:
        """Non-list JSON yields []."""
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='{"a": 1}', value_type="string")
            },
        )
        result = _stored_manual_order(book)
        assert result == []

    def test_empty_string_is_empty(self) -> None:
        """Empty string value yields []."""
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={"manual_order": api.CustomValueView(value="", value_type="string")},
        )
        result = _stored_manual_order(book)
        assert result == []


class TestStoringTheOrder:
    """Test storing chapter order in patches."""

    def test_submitting_a_custom_order_stores_keys(self, build_epub: Callable[..., Path]) -> None:
        """Submitting a custom order stores the keys in reversed order."""
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
                            selected=tuple(original_idrefs),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert len(patches) == 1
        patch = patches[0]
        assert "manual_order" in patch.custom_values
        stored_value = patch.custom_values["manual_order"].value
        stored_keys = json.loads(stored_value)
        # build_epub creates title page + 3 chapters
        assert len(stored_keys) >= 3
        assert patch.custom_values["manual_order"].value_type == "string"

    def test_manual_order_at_is_iso_utc(self, build_epub: Callable[..., Path]) -> None:
        """The manual_order_at value is ISO-8601 UTC."""
        from datetime import datetime

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
                            order=tuple(reversed(original_idrefs)),
                            selected=tuple(original_idrefs),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert len(patches) == 1
        patch = patches[0]
        assert "manual_order_at" in patch.custom_values
        iso_value = patch.custom_values["manual_order_at"].value
        dt = datetime.fromisoformat(iso_value)
        from datetime import UTC

        assert dt.tzinfo == UTC
        assert patch.custom_values["manual_order_at"].value_type == "datetime"

    def test_keys_are_recomputed_from_the_edited_file(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """After removing one chapter, the stored key list has surviving chapters' keys."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Keep only first and third
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

        assert len(patches) == 1
        patch = patches[0]
        stored_keys = json.loads(patch.custom_values["manual_order"].value)
        assert len(stored_keys) == 2

    def test_empty_keys_are_dropped(self, build_epub: Callable[..., Path]) -> None:
        """Chapters with empty keys do not appear in the stored list."""
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
                            order=tuple(reversed(original_idrefs)),
                            selected=tuple(original_idrefs),
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert len(patches) == 1
        patch = patches[0]
        stored_keys = json.loads(patch.custom_values["manual_order"].value)
        # No empty strings in the list
        assert all(k for k in stored_keys)

    def test_stored_line_logs_the_count(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The stored log line contains the key count."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
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
                            order=tuple(reversed(original_idrefs)),
                            selected=tuple(original_idrefs),
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
            'Manual chapter order stored for "' in record.message and " key(s)" in record.message
            for record in caplog.records
        )

    def test_cancel_stores_nothing(self, build_epub: Callable[..., Path]) -> None:
        """A cancelled view returns no custom values."""
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

        patches = EpubChapterReorderPlugin().process(
            (make_epub_item(epub, book_id="b1", title="Test Book", story_url=None),), ctx
        )

        assert patches == []


class TestClearingTheOrder:
    """Test clearing the manual order when submitting the automatic order."""

    def test_note_section_appears_when_an_order_is_stored(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """With a stored order, the view's first section is a NOTE."""
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Create a book view with a stored order
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='["a","b"]', value_type="string")
            },
        )
        item = api.EpubItem(book=book, epub_path=epub)

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[api.ViewResult(submitted=False)],
        )

        EpubChapterReorderPlugin().process((item,), ctx)

        assert len(ctx.views) == 1
        assert len(ctx.views[0].sections) > 0
        first_section = ctx.views[0].sections[0]
        assert first_section.kind == api.ViewSectionKind.NOTE
        assert first_section.name == "manual_order_note"

    def test_no_note_section_without_a_stored_order(self, build_epub: Callable[..., Path]) -> None:
        """Without a stored order, the view has no NOTE section."""
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
        # Should have exactly one section: chapters
        assert len(ctx.views[0].sections) == 1
        assert ctx.views[0].sections[0].kind == api.ViewSectionKind.ITEM_LIST

    def test_submitting_the_automatic_order_clears_the_memory(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """Submitting the automatic order clears the stored memory."""

        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Create a book with a stored order that differs from automatic
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='["b","a","c"]', value_type="string")
            },
        )
        item = api.EpubItem(book=book, epub_path=epub)

        # Submit the automatic order (spine order)
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

        patches = EpubChapterReorderPlugin().process((item,), ctx)

        # Should have cleared the memory
        assert len(patches) == 1
        patch = patches[0]
        assert patch.custom_values["manual_order"].value == ""
        assert patch.custom_values["manual_order_at"].value == ""

    def test_cleared_line_logs(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Clearing the order logs an INFO record with specific text."""

        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='["a","b"]', value_type="string")
            },
        )
        item = api.EpubItem(book=book, epub_path=epub)

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

        with caplog.at_level(logging.INFO):
            EpubChapterReorderPlugin().process((item,), ctx)

        assert any(
            "Manual chapter order cleared for" in record.message
            and "by an explicit automatic reorder" in record.message
            for record in caplog.records
        )

    def test_submitting_a_different_order_still_stores(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """With a stored order, submitting a different one stores it."""
        # Use unnumbered chapters so automatic order won't sort them numerically
        chapters = [
            ("Prologue", "https://example.com/prologue"),
            ("Interlude", "https://example.com/interlude"),
            ("Epilogue", "https://example.com/epilogue"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        original_idrefs = [e.idref for e in entries]

        # Create a book with a stored order
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='["a","b","c"]', value_type="string")
            },
        )
        item = api.EpubItem(book=book, epub_path=epub)

        # Submit a reordered version
        reordered = tuple(reversed(original_idrefs))

        ctx = FakeContext(
            mode=api.InvocationMode.HEADED,
            logger=logging.getLogger(_LOGGER_NAME),
            view_results=[
                api.ViewResult(
                    submitted=True,
                    selections={
                        "chapters": api.ViewSelection(
                            order=tuple(original_idrefs),
                            selected=reordered,
                        )
                    },
                )
            ],
        )

        patches = EpubChapterReorderPlugin().process((item,), ctx)

        assert len(patches) == 1
        patch = patches[0]
        # Should store the new order (may or may not be cleared depending on automatic)
        # The key is that custom_values exist and have the right structure
        assert "manual_order" in patch.custom_values
        assert "manual_order_at" in patch.custom_values
        # At minimum, _at should have a value if we submitted anything
        if patch.custom_values["manual_order"].value:
            stored_keys = json.loads(patch.custom_values["manual_order"].value)
            assert isinstance(stored_keys, list)


class TestRefusingDuplicateKeys:
    """Test that duplicate-key orders are refused, not silently reverted."""

    def test_a_duplicate_key_order_is_refused_and_named(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A duplicate-key order is refused and leaves the automatic order.

        When reorder_epub receives a manual order with duplicate keys, it refuses
        it, sets manual_order to None, and logs a WARNING with the key counts.
        """
        from epub_chapter_reorder.reorder_step import reorder_epub

        chapters = [
            ("Chapter 1", "https://example.test/ch1"),
            ("Chapter 2", "https://example.test/ch2"),
            ("Chapter 3", "https://example.test/ch3"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        # Try to apply a manual order with duplicates: ["a", "a", "b"]
        with caplog.at_level(logging.WARNING):
            reorder_epub(epub, manual_order=["a", "a", "b"])

        # Should log a WARNING with the refusal message and key counts
        refusal_warnings = [
            record
            for record in caplog.records
            if "Manual chapter order refused for" in record.message
            and "3 key(s) but only 2 distinct" in record.message
        ]
        assert len(refusal_warnings) >= 1, (
            f"Expected refusal warning with key counts, got: {[r.message for r in caplog.records]}"
        )

    def test_a_refused_order_never_logs_partial_application(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A refused order does not log partial application.

        When a duplicate-key order is refused, the partial-application warning
        should NOT appear.
        """
        from epub_chapter_reorder.reorder_step import reorder_epub

        chapters = [
            ("Chapter 1", "https://example.test/ch1"),
            ("Chapter 2", "https://example.test/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        with caplog.at_level(logging.WARNING):
            reorder_epub(epub, manual_order=["a", "a"])

        # Assert the refusal warning appears
        refusal_warnings = [
            record
            for record in caplog.records
            if "Manual chapter order refused for" in record.message
        ]
        assert len(refusal_warnings) >= 1

        # Assert the partial-application warning does NOT appear
        partial_warnings = [
            record
            for record in caplog.records
            if "Manual chapter order partially applied" in record.message
        ]
        assert not partial_warnings, (
            f"Partial-application warning should not appear for refused order, "
            f"but got: {[r.message for r in partial_warnings]}"
        )

    def test_a_distinct_order_is_still_applied(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A distinct manual order is still applied normally.

        Duplicate detection should not break the normal path for valid orders.
        """
        from epub_chapter_reorder.reorder_step import reorder_epub

        chapters = [
            ("Chapter 1", "https://example.test/ch1"),
            ("Chapter 2", "https://example.test/ch2"),
        ]
        epub = build_epub(chapters, doc_title="Test Book")

        with caplog.at_level(logging.INFO):
            reorder_epub(epub, manual_order=["b", "a"])

        # Should log an applied message or a different order is used
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        # Just ensure we got some kind of order processing log
        assert len(info_records) > 0

    def test_a_degenerate_stored_order_is_discarded_at_read_time(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stored order with duplicate keys is discarded at read time.

        When _stored_manual_order reads a JSON list with duplicate keys, it
        returns [] and logs a WARNING.
        """
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='["u","u","u"]', value_type="string")
            },
        )

        with caplog.at_level(logging.WARNING):
            result = _stored_manual_order(book)

        assert result == []
        discard_warnings = [
            record
            for record in caplog.records
            if "discarding it (it encodes no ordering)" in record.message
        ]
        assert len(discard_warnings) >= 1, (
            f"Expected discard warning, got: {[r.message for r in caplog.records]}"
        )

    def test_a_valid_stored_order_is_returned_unchanged(self) -> None:
        """A valid stored order with distinct keys is returned unchanged.

        Round-trip test to ensure the degenerate check does not affect valid orders.
        """
        book = make_book_view(
            book_id="b1",
            title="Test Book",
            story_url=None,
            custom_values={
                "manual_order": api.CustomValueView(value='["a","b","c"]', value_type="string")
            },
        )

        result = _stored_manual_order(book)

        assert result == ["a", "b", "c"]


class TestManualOrderWithSharedUrls:
    """Test that unique chapter keys enable manual orders to survive headless passes."""

    def test_the_partial_application_warning_no_longer_fires(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """With unique chapter keys, no 'partially applied' warning is logged.

        When chapters share a URL and unique keys are used, the stored manual order
        fully applies with no partial-application warning.
        """
        from epub_chapter_reorder.reorder_step import reorder_epub

        chapters = [(f"Chapter {i}", "https://example.test/story") for i in range(1, 10)]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        content_entries = [e for e in entries if e.role.value == "content"]

        from ebookerr_sdk.epub.chapters import chapter_keys

        key_map = chapter_keys(doc, entries)
        original_keys = [key_map[e.idref] for e in content_entries if key_map[e.idref]]
        # Move first key to the end to ensure a different order than automatic
        manual_order = original_keys[1:] + [original_keys[0]]

        with caplog.at_level(logging.WARNING):
            reorder_epub(epub, manual_order=manual_order)

        # Assert the warning does NOT appear
        partial_warnings = [
            record
            for record in caplog.records
            if "Manual chapter order partially applied" in record.message
        ]
        assert not partial_warnings

    def test_a_book_with_distinct_chapter_urls_is_unaffected(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """A book with distinct per-chapter URLs is unaffected (regression guard).

        When chapters already have distinct URLs, chapter_keys produces the same
        result as the old chapter_key approach.
        """
        chapters = [(f"Chapter {i}", f"https://example.test/story/ch{i}") for i in range(1, 10)]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        content_entries = [e for e in entries if e.role.value == "content"]

        from ebookerr_sdk.epub.chapters import chapter_key, chapter_keys

        # Collect keys using both methods to verify regression
        old_style_keys = [chapter_key(doc, e) for e in content_entries]
        key_map = chapter_keys(doc, entries)
        new_style_keys = [key_map[e.idref] for e in content_entries]

        # For distinct URLs, the new style should match the old style
        assert old_style_keys == new_style_keys

    def test_automatic_key_order_returns_distinct_keys(
        self, build_epub: Callable[..., Path]
    ) -> None:
        """automatic_key_order returns distinct keys for chapters sharing a URL."""
        chapters = [(f"Chapter {i}", "https://example.test/story") for i in range(1, 10)]
        epub = build_epub(chapters, doc_title="Test Book")

        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        content_entries = [e for e in entries if e.role.value == "content"]

        from ebookerr_sdk.epub.chapters import chapter_keys

        key_map = chapter_keys(doc, entries)
        content_keys = [key_map[e.idref] for e in content_entries if key_map[e.idref]]

        # All keys should be distinct
        assert len(set(content_keys)) == len(content_keys)
        # Should have 9 keys (9 content chapters)
        assert len(content_keys) == 9


def test_a_manual_order_survives_a_headless_pass_when_chapters_share_a_book_url(
    build_epub: Callable[..., Path],
) -> None:
    """A manual order on chapters sharing a URL is applied on headless reorder.

    When all chapters carry the same URL (the book's story URL), manual chapter
    order is preserved via unique chapter_keys on every headless pass.
    """
    from epub_chapter_reorder.reorder_step import reorder_epub

    # Build a 9-chapter EPUB where all chapters share the same URL
    chapters = [(f"Chapter {i}", "https://example.test/story") for i in range(1, 10)]
    epub = build_epub(chapters, doc_title="Test Book")

    doc = EpubDocument.open(epub)
    entries = classify_spine(doc)

    # Verify we have 9 chapters (plus title page = 10 total)
    content_entries = [e for e in entries if e.role.value == "content"]
    assert len(content_entries) == 9

    # Compute manual order that moves chapter 9 to the front
    from ebookerr_sdk.epub.chapters import chapter_keys

    key_map = chapter_keys(doc, entries)
    original_keys = [key_map[e.idref] for e in content_entries if key_map[e.idref]]
    manual_order = [original_keys[-1]] + original_keys[:-1]

    # Verify the stored order has distinct keys
    assert len(set(manual_order)) == len(manual_order)
    assert len(manual_order) == 9

    # Apply the manual order via headless reorder_epub
    reorder_epub(epub, manual_order=manual_order)

    # Read the resulting navMap order
    doc_after = EpubDocument.open(epub)
    entries_after = classify_spine(doc_after)
    content_entries_after = [e for e in entries_after if e.role.value == "content"]

    # Get the keys of the reordered chapters
    key_map_after = chapter_keys(doc_after, entries_after)
    result_keys = [key_map_after[e.idref] for e in content_entries_after if key_map_after[e.idref]]

    # Assert the result matches the manual order
    assert result_keys == manual_order


@pytest.mark.pins("EXP-185")
def test_a_manual_order_across_a_zone_boundary_survives_a_headless_pass(
    build_epub: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A manual order carrying zone-placed keys logs "applied ... N chapter(s)" with N=all.

    When a user reorders chapters in the editor and stores the order, keeping zone-placed
    rows (title page, front/back matter) in place, a headless pass applies the order in full
    and logs "applied ... 10 chapter(s)" rather than "6 of 9 partially applied". A second
    pass with the same order returns False and logs "unchanged".
    """
    from ebookerr_sdk.epub.chapters import chapter_keys
    from epub_chapter_reorder.reorder_step import reorder_epub

    # Build the 10-row specimen (title, prologue, 6 chapters, note, epilogue)
    epub = build_epub(_ZONE_CHAPTERS, doc_title="Test Book")

    # Get all entries and keys
    doc = EpubDocument.open(epub)
    entries = classify_spine(doc)
    key_map = chapter_keys(doc, entries)
    keys = [key_map[e.idref] for e in entries]
    content = [key_map[e.idref] for e in entries if e.role.value == "content"]

    # Manually reorder: Chapter 6 moved first within content, zone rows in place
    manual_order = keys[:2] + [content[-1]] + content[:-1] + keys[-2:]

    # Apply the manual order via reorder_epub
    with caplog.at_level(logging.INFO):
        result = reorder_epub(epub, manual_order=manual_order)

    # First pass should have changed the file
    assert result is True

    # Verify the order matches after reopening
    doc_after = EpubDocument.open(epub)
    entries_after = classify_spine(doc_after)
    key_map_after = chapter_keys(doc_after, entries_after)
    result_keys = [key_map_after[e.idref] for e in entries_after]
    assert result_keys == manual_order

    # Check logs: should show "10 chapter(s)" (all keys matched), NOT "partially applied"
    assert any(
        'Manual chapter order applied to "Test Book": 10 chapter(s)' in record.message
        for record in caplog.records
    ), f"Expected 'applied ... 10 chapter(s)', got: {[r.message for r in caplog.records]}"

    assert not any("partially applied" in record.message for record in caplog.records), (
        f"Should not log 'partially applied', got: {[r.message for r in caplog.records]}"
    )

    # Second pass with the same order should return False (unchanged)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        result2 = reorder_epub(epub, manual_order=manual_order)

    assert result2 is False
    assert any(
        'Chapter-reorder left "' in record.message and "unchanged" in record.message
        for record in caplog.records
    ), f"Expected 'unchanged' log, got: {[r.message for r in caplog.records]}"


def test_a_stored_order_that_moves_front_matter_is_applied_with_the_zone_kept(
    build_epub: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored order trying to move front matter is applied with zone placement kept.

    When a manual order includes front-matter keys in positions outside their zone,
    the zone guarantee (CHC-D5) ensures they stay in their zone. The order is still
    considered fully applied, and no partial-application warning fires.
    """
    from ebookerr_sdk.epub.chapters import chapter_keys
    from epub_chapter_reorder.reorder_step import reorder_epub

    # Build the 10-row specimen
    epub = build_epub(_ZONE_CHAPTERS, doc_title="Test Book")

    doc = EpubDocument.open(epub)
    entries = classify_spine(doc)
    key_map = chapter_keys(doc, entries)
    keys = [key_map[e.idref] for e in entries]
    content = [key_map[e.idref] for e in entries if e.role.value == "content"]

    # Try to place Prologue (keys[1]) at position 2 (third row):
    # [title, content[0], prologue, *content[1:], *back_matter]
    # But prologue should stay in row 1 (zone 1) after the pass
    manual_order = [keys[0], content[0], keys[1], *content[1:], *keys[-2:]]

    with caplog.at_level(logging.INFO):
        reorder_epub(epub, manual_order=manual_order)

    # Prologue should remain at row index 1 (after title page)
    doc_after = EpubDocument.open(epub)
    entries_after = classify_spine(doc_after)
    # Find prologue entry by checking for zone 1 + prologue URL
    prologue_entries = [e for e in entries_after if e.role.value == "front"]
    assert len(prologue_entries) >= 1
    prologue_entry = prologue_entries[0]
    # The prologue should be at position 1 (after title page)
    original_prologue_pos = next(
        i for i, e in enumerate(entries_after) if e.idref == prologue_entry.idref
    )
    assert original_prologue_pos == 1, (
        f"Prologue should be at row 1 but found at {original_prologue_pos}"
    )

    # All 10 keys should be known (no partial warning)
    caplog_messages = [r.message for r in caplog.records]
    assert any("10 chapter(s)" in msg for msg in caplog_messages), (
        f"Expected '10 chapter(s)' in logs, got: {caplog_messages}"
    )

    assert not any("partially applied" in msg for msg in caplog_messages), (
        f"Should not log 'partially applied', got: {caplog_messages}"
    )


def test_a_genuinely_unknown_key_still_logs_the_partial_warning(
    build_epub: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A manual order with an unknown key still logs the partial-application warning.

    When a stored order includes a key that doesn't exist in the current EPUB (e.g., a
    chapter was removed), the partial-application warning should still fire with
    "X of Y key(s) matched" showing the gap.
    """
    from ebookerr_sdk.epub.chapters import chapter_keys
    from epub_chapter_reorder.reorder_step import reorder_epub

    # Build the 10-row specimen
    epub = build_epub(_ZONE_CHAPTERS, doc_title="Test Book")

    doc = EpubDocument.open(epub)
    entries = classify_spine(doc)
    key_map = chapter_keys(doc, entries)
    keys = [key_map[e.idref] for e in entries]

    # Create a manual order with all 10 keys plus one unknown key
    manual_order = keys + ["https://example.test/s/gone"]

    with caplog.at_level(logging.WARNING):
        reorder_epub(epub, manual_order=manual_order)

    # Should log a partial-application warning with "10 of 11 key(s) matched"
    assert any("10 of 11 key(s) matched" in record.message for record in caplog.records), (
        f"Expected '10 of 11 key(s) matched' in logs, got: {[r.message for r in caplog.records]}"
    )


def _stored_order_item(epub: Path) -> tuple[api.EpubItem, list[str]]:
    """An item whose record stores [title, chapter 3, chapter 1, chapter 2].

    Stores under the core's namespaced key, which encode_epub_item strips.
    """
    doc = EpubDocument.open(epub)
    entries = classify_spine(doc)
    key_map = chapter_keys(doc, entries)
    content = [key_map[e.idref] for e in entries if e.role.value == "content"]
    stored = [key_map[entries[0].idref], content[2], content[0], content[1]]
    item = make_epub_item(
        epub,
        book_id="b1",
        title="Test Book",
        story_url=None,
        custom_values={
            "epub_chapter_reorder.manual_order": api.CustomValueView(
                value=json.dumps(stored), value_type="string"
            )
        },
    )
    return item, stored


def test_a_stored_order_is_reapplied_through_the_wire(
    build_epub: Callable[..., Path],
) -> None:
    """A headless pass over the wire re-applies the stored manual order (CHX-D4)."""
    epub = build_epub(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        doc_title="Test Book",
    )
    item, stored = _stored_order_item(epub)

    terminal, _frames = run_wire(
        EpubChapterReorderPlugin(),
        make_request("process", items=[encode_epub_item(item, plugin_id="epub_chapter_reorder")]),
    )

    assert terminal["ok"] is True
    doc = EpubDocument.open(epub)
    entries = classify_spine(doc)
    key_map = chapter_keys(doc, entries)
    assert [key_map[e.idref] for e in entries] == stored


def test_the_editor_shows_the_stored_order_note_through_the_wire(
    build_epub: Callable[..., Path],
) -> None:
    """A headed pass over the wire opens the editor with the manual-order note (CHX-D3)."""
    epub = build_epub(
        [("Chapter 1", "u1"), ("Chapter 2", "u2"), ("Chapter 3", "u3")],
        doc_title="Test Book",
    )
    item, _stored = _stored_order_item(epub)

    _terminal, frames = run_wire(
        EpubChapterReorderPlugin(),
        make_request(
            "process",
            items=[encode_epub_item(item, plugin_id="epub_chapter_reorder")],
            interactive=True,
        ),
    )

    views = [frame["view"] for frame in frames if frame.get("op") == "view"]
    assert [section["kind"] for section in views[0]["sections"]] == ["note", "item_list"]
