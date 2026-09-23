"""Tests for EpubChapterReorderPlugin ( FR-MIG-5)."""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.testing import FakeContext, make_epub_item
from epub_chapter_reorder.plugin import EpubChapterReorderPlugin

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


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class TestManifest:
    def test_id(self) -> None:
        assert EpubChapterReorderPlugin().manifest.id == "epub_chapter_reorder"

    def test_type_is_epub(self) -> None:
        assert EpubChapterReorderPlugin().manifest.plugin_type == api.PluginType.EPUB

    def test_subscribes_to_epub_created(self) -> None:
        assert api.PluginEventType.EPUB_CREATED in EpubChapterReorderPlugin().manifest.events

    def test_subscribes_to_epub_modified(self) -> None:
        assert api.PluginEventType.EPUB_MODIFIED in EpubChapterReorderPlugin().manifest.events

    def test_priority_is_one_hundred(self) -> None:
        assert EpubChapterReorderPlugin().manifest.priority == 100

    def test_headless(self) -> None:
        assert EpubChapterReorderPlugin().manifest.headless is True

    def test_settings_schema_empty(self) -> None:
        assert EpubChapterReorderPlugin().settings_schema() == api.SettingsSchema()

    def test_satisfies_epub_plugin_protocol(self) -> None:
        assert isinstance(EpubChapterReorderPlugin(), api.EpubPlugin)

    def test_plugin_is_both_headed_and_headless(self) -> None:
        manifest = EpubChapterReorderPlugin().manifest
        assert manifest.headed is True
        assert manifest.headless is True

    def test_id_and_name_are_unchanged(self) -> None:
        manifest = EpubChapterReorderPlugin().manifest
        assert manifest.id == "epub_chapter_reorder"
        assert manifest.name == "Chapter Reorder"

    def test_version_is_2_1_0(self) -> None:
        assert EpubChapterReorderPlugin().manifest.version == "2.1.0"

    def test_declares_one_book_selection_trigger(self) -> None:
        manifest = EpubChapterReorderPlugin().manifest
        assert len(manifest.ui_triggers) == 1
        trigger = manifest.ui_triggers[0]
        assert trigger.scope == "book_selection_action"
        assert trigger.icon == "low_priority"
        assert trigger.label == "Chapters"
        assert trigger.min_books == 1
        assert trigger.filters == ()

    def test_priority_and_events_are_unchanged(self) -> None:
        manifest = EpubChapterReorderPlugin().manifest
        assert manifest.priority == 100
        assert manifest.events == (
            api.PluginEventType.EPUB_CREATED,
            api.PluginEventType.EPUB_MODIFIED,
        )

    def test_declares_only_the_timestamp_custom_value(self) -> None:
        manifest = EpubChapterReorderPlugin().manifest
        assert len(manifest.custom_values) == 1
        custom_value = manifest.custom_values[0]
        assert custom_value.key == "manual_order_at"
        assert custom_value.type == "datetime"
        assert custom_value.label == "Manual chapter order set"

    def test_manual_order_is_not_declared(self) -> None:
        manifest = EpubChapterReorderPlugin().manifest
        assert not any(cv.key == "manual_order" for cv in manifest.custom_values)


# ---------------------------------------------------------------------------
# process() — reorder
# ---------------------------------------------------------------------------


class TestProcess:
    def test_out_of_order_epub_is_reordered(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        item = make_epub_item(epub, book_id="b1", title="Tending Bar", story_url=None)
        EpubChapterReorderPlugin().process(
            (item,), FakeContext(logger=logging.getLogger("plugin.epub_chapter_reorder"))
        )
        doc = EpubDocument.open(epub)
        assert [p.id for p in doc.ncx.nav_points()] == _SORTED
        assert [s.idref for s in doc.opf.spine()] == _SORTED

    def test_out_of_order_returns_book_patch_with_num_chapters(
        self, build_epub: Callable[..., Path]
    ) -> None:
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")
        item = make_epub_item(epub, book_id="book-42", title="Tending Bar", story_url=None)
        patches = EpubChapterReorderPlugin().process(
            (item,), FakeContext(logger=logging.getLogger("plugin.epub_chapter_reorder"))
        )
        assert len(patches) == 1
        assert patches[0].book_id == "book-42"
        assert "num_chapters" in patches[0].fields
        # 6 content chapters (title page is excluded by content_chapter_count)
        assert patches[0].fields["num_chapters"] == 6

    def test_already_ordered_returns_empty(self, build_epub: Callable[..., Path]) -> None:
        ordered = [_TENDING_BAR[i] for i in (2, 3, 4, 5, 0, 1)]  # Pt.01..04, Part 5, Part 6
        epub = build_epub(ordered, doc_title="Tending Bar")
        before = epub.read_bytes()
        item = make_epub_item(epub, book_id="b1", title="Tending Bar", story_url=None)
        patches = EpubChapterReorderPlugin().process(
            (item,), FakeContext(logger=logging.getLogger("plugin.epub_chapter_reorder"))
        )
        assert patches == []
        assert epub.read_bytes() == before

    def test_malformed_epub_logs_warning_and_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = tmp_path / "bad.epub"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")  # no container.xml
        item = make_epub_item(bad, book_id="b1", title="Tending Bar", story_url=None)
        with caplog.at_level(logging.WARNING):
            patches = EpubChapterReorderPlugin().process(
                (item,), FakeContext(logger=logging.getLogger("plugin.epub_chapter_reorder"))
            )
        assert patches == []
        assert any("reorder" in record.message.lower() for record in caplog.records)

    def test_malformed_epub_does_not_raise(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.epub"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
        item = make_epub_item(bad, book_id="b1", title="Tending Bar", story_url=None)
        # Must not raise
        EpubChapterReorderPlugin().process(
            (item,), FakeContext(logger=logging.getLogger("plugin.epub_chapter_reorder"))
        )

    def test_multiple_items_patches_only_changed(self, build_epub: Callable[..., Path]) -> None:
        out_of_order = build_epub(_TENDING_BAR, doc_title="Tending Bar", filename="ooo.epub")
        already_sorted = build_epub(
            [_TENDING_BAR[i] for i in (2, 3, 4, 5, 0, 1)],
            doc_title="Tending Bar",
            filename="sorted.epub",
        )
        items = (
            make_epub_item(out_of_order, book_id="book-a", title="Tending Bar", story_url=None),
            make_epub_item(already_sorted, book_id="book-b", title="Tending Bar", story_url=None),
        )
        patches = EpubChapterReorderPlugin().process(
            items, FakeContext(logger=logging.getLogger("plugin.epub_chapter_reorder"))
        )
        assert len(patches) == 1
        assert patches[0].book_id == "book-a"

    def test_plugin_passes_the_record_title_down(
        self, build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The plugin should pass item.book.title as display_title to reorder_epub."""
        epub = build_epub(_TENDING_BAR, doc_title="Tending Bar")

        # Track calls to reorder_epub
        captured_kwargs = []
        chapter_reorder_module = __import__(
            "epub_chapter_reorder.reorder_step", fromlist=["reorder_epub"]
        )
        original_reorder_epub = chapter_reorder_module.reorder_epub

        def mock_reorder_epub(*args: Any, **kwargs: Any) -> bool:
            captured_kwargs.append(kwargs)
            return original_reorder_epub(*args, **kwargs)

        monkeypatch.setattr(
            "epub_chapter_reorder.plugin.reorder_epub",
            mock_reorder_epub,
        )

        item = make_epub_item(epub, book_id="book-1", title="Edited Title", story_url=None)

        EpubChapterReorderPlugin().process(
            (item,), FakeContext(logger=logging.getLogger("plugin.epub_chapter_reorder"))
        )

        # Check that reorder_epub was called with display_title="Edited Title"
        assert len(captured_kwargs) > 0
        assert captured_kwargs[0].get("display_title") == "Edited Title"
