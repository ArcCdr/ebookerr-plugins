"""Tests for EpubNormalizePlugin."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.testing import Cancelled, FakeContext

from epub_normalize.plugin import EpubNormalizePlugin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    epub_path: Path,
    book_id: str = "b1",
    title: str = "Test Book",
) -> api.EpubItem:
    """Build an EpubItem around a given path."""
    return api.EpubItem(
        book=api.BookView(
            book_id=book_id,
            title=title,
            author="Test Author",
            story_url="https://example.com",
            output_filename="test.epub",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external=api.ExternalLink(),
            progress=api.ExternalProgress(),
            custom_values={},
        ),
        epub_path=epub_path,
    )


def _set_css(epub_path: Path, css: str) -> None:
    """Rewrite the stylesheet of an EPUB."""
    doc = EpubDocument.open(epub_path)
    doc.write_resource("OEBPS/stylesheet.css", css.encode("utf-8"))
    doc.save()


def _get_css(epub_path: Path) -> str:
    """Read the stylesheet of an EPUB."""
    doc = EpubDocument.open(epub_path)
    return doc.resource_bytes("OEBPS/stylesheet.css").decode("utf-8")


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_identity(self) -> None:
        plugin = EpubNormalizePlugin()
        assert plugin.manifest.id == "epub_normalize"
        assert plugin.manifest.name == "EPUB Normalize"
        assert plugin.manifest.plugin_type is api.PluginType.EPUB
        assert plugin.manifest.version == "1.1.0"

    def test_manifest_pipeline_position(self) -> None:
        plugin = EpubNormalizePlugin()
        assert plugin.manifest.priority == 70

    def test_manifest_priority_is_seventy(self) -> None:
        plugin = EpubNormalizePlugin()
        assert plugin.manifest.priority == 70

    def test_manifest_events(self) -> None:
        plugin = EpubNormalizePlugin()
        assert plugin.manifest.events == (
            api.PluginEventType.EPUB_CREATED,
            api.PluginEventType.EPUB_MODIFIED,
        )

    def test_manifest_runs_headless_and_headed(self) -> None:
        plugin = EpubNormalizePlugin()
        assert plugin.manifest.headless is True
        assert plugin.manifest.headed is True

    def test_manifest_ui_trigger(self) -> None:
        plugin = EpubNormalizePlugin()
        triggers = plugin.manifest.ui_triggers
        assert len(triggers) == 1
        trigger = triggers[0]
        assert trigger.scope == "book_selection_action"
        assert trigger.icon == "sweep"
        assert trigger.label == "Normalize styling"
        assert trigger.min_books == 1

    def test_manifest_declares_no_exclusive_group(self) -> None:
        plugin = EpubNormalizePlugin()
        assert plugin.manifest.exclusive_group is None

    def test_settings_schema_keys_and_defaults(self) -> None:
        plugin = EpubNormalizePlugin()
        schema = plugin.settings_schema()
        assert len(schema.fields) == 7
        fields_dict = {f.key: f for f in schema.fields}
        assert list(fields_dict.keys()) == [
            "strip_fonts",
            "strip_colors",
            "convert_font_sizes",
            "strip_line_spacing",
            "strip_fixed_dimensions",
            "preserve_alignment",
            "normalize_margins",
        ]
        for key in fields_dict:
            assert fields_dict[key].type == "bool"
            assert fields_dict[key].label
            assert fields_dict[key].help
        assert fields_dict["strip_fonts"].default is True
        assert fields_dict["strip_colors"].default is True
        assert fields_dict["convert_font_sizes"].default is True
        assert fields_dict["strip_line_spacing"].default is True
        assert fields_dict["strip_fixed_dimensions"].default is True
        assert fields_dict["preserve_alignment"].default is True
        assert fields_dict["normalize_margins"].default is False

    def test_settings_schema_is_the_same_object_as_the_manifest_declares(self) -> None:
        plugin = EpubNormalizePlugin()
        assert plugin.settings_schema() is plugin.manifest.settings_schema


# ---------------------------------------------------------------------------
# Behaviour tests
# ---------------------------------------------------------------------------


class TestBehaviour:
    def test_process_normalizes_the_staged_file(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "body { font-family: Georgia; margin: 2% }")
        item = _make_item(epub)

        plugin = EpubNormalizePlugin()
        result = plugin.process((item,), FakeContext())

        assert result == []
        css = _get_css(epub)
        assert "Georgia" not in css

    def test_process_returns_no_patches(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "body { color: red; }")
        item = _make_item(epub)

        with caplog.at_level(logging.INFO):
            plugin = EpubNormalizePlugin()
            result = plugin.process((item,), FakeContext())

        assert result == []

    def test_settings_are_honoured(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "body { font-family: Georgia; }")
        item = _make_item(epub)

        plugin = EpubNormalizePlugin()
        plugin.process((item,), FakeContext(settings={"strip_fonts": False}))

        css = _get_css(epub)
        assert "Georgia" in css

    def test_absent_settings_fall_back_to_defaults(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "body { color: red; }")
        item = _make_item(epub)

        plugin = EpubNormalizePlugin()
        plugin.process((item,), FakeContext(settings={}))

        css = _get_css(epub)
        assert "red" not in css

    def test_normalize_margins_defaults_off(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "p { margin: 20px }")
        item = _make_item(epub)

        plugin = EpubNormalizePlugin()
        plugin.process((item,), FakeContext(settings={}))

        css = _get_css(epub)
        assert "20px" in css

    def test_normalize_margins_can_be_enabled(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "p { margin: 20px }")
        item = _make_item(epub)

        plugin = EpubNormalizePlugin()
        plugin.process((item,), FakeContext(settings={"normalize_margins": True}))

        css = _get_css(epub)
        assert "1.25em" in css

    def test_info_line_on_a_changed_book(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "body { font-family: Georgia; color: red; }")
        item = _make_item(epub, book_id="b1", title="Test Book")

        with caplog.at_level(logging.INFO):
            plugin = EpubNormalizePlugin()
            plugin.process((item,), FakeContext())

        records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(records) == 1
        assert 'Normalized "Test Book" (book_id=b1)' in records[0].message
        assert "declaration(s)" in records[0].message
        assert " in " in records[0].message

    def test_info_line_on_a_no_op_book(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        item = _make_item(epub)

        with caplog.at_level(logging.INFO):
            plugin = EpubNormalizePlugin()
            plugin.process((item,), FakeContext())

        records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(records) == 1
        assert 'Normalize no-op for "Test Book" (book_id=b1)' in records[0].message

    def test_warning_on_a_fixed_layout_book(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        from ebookerr_sdk.epub.opf import PackageDocument

        epub = build_epub([("Chapter One", "Test content")])
        doc = EpubDocument.open(epub)
        opf_xml = doc.opf.to_xml()
        opf_xml = opf_xml.replace(
            "</metadata>",
            '<meta property="rendition:layout">pre-paginated</meta></metadata>',
        )
        doc.opf = PackageDocument.parse(opf_xml)
        doc.save()
        item = _make_item(epub)

        with caplog.at_level(logging.WARNING):
            plugin = EpubNormalizePlugin()
            plugin.process((item,), FakeContext())

        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        msg = 'Normalize skipped for "Test Book" (book_id=b1): fixed-layout book'
        assert msg in records[0].message

    def test_warning_on_a_malformed_epub(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        malformed = tmp_path / "broken.epub"
        malformed.write_bytes(b"not a zip")
        item = _make_item(malformed)

        with caplog.at_level(logging.WARNING):
            plugin = EpubNormalizePlugin()
            result = plugin.process((item,), FakeContext())

        assert result == []
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert "Normalize skipped for" in records[0].message
        assert "(malformed EPUB)" in records[0].message

    def test_a_broken_book_does_not_stop_the_batch(
        self, build_epub: Callable[..., Path], tmp_path: Path
    ) -> None:
        malformed = tmp_path / "broken.epub"
        malformed.write_bytes(b"not a zip")
        item1 = _make_item(malformed)

        good_epub = build_epub([("Chapter One", "Test content")])
        _set_css(good_epub, "body { color: red; }")
        item2 = _make_item(good_epub)

        plugin = EpubNormalizePlugin()
        result = plugin.process((item1, item2), FakeContext())

        assert result == []
        css = _get_css(good_epub)
        assert "red" not in css

    def test_cancellation_is_honoured(self, build_epub: Callable[..., Path]) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        original_bytes = epub.read_bytes()
        item = _make_item(epub)

        plugin = EpubNormalizePlugin()
        with pytest.raises(Cancelled):
            plugin.process((item,), FakeContext(cancel_after=0))

        assert epub.read_bytes() == original_bytes

    def test_second_process_is_a_no_op(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        _set_css(epub, "body { color: red; }")
        item = _make_item(epub)

        plugin = EpubNormalizePlugin()
        plugin.process((item,), FakeContext())
        bytes_after_first = epub.read_bytes()

        caplog.clear()
        with caplog.at_level(logging.INFO):
            plugin.process((item,), FakeContext())
        bytes_after_second = epub.read_bytes()

        assert bytes_after_second == bytes_after_first
        records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(records) == 1
        assert "Normalize no-op for" in records[0].message

    def test_title_falls_back_when_missing(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        item = _make_item(epub, title=None)

        with caplog.at_level(logging.INFO):
            plugin = EpubNormalizePlugin()
            plugin.process((item,), FakeContext())

        records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(records) == 1
        assert "(unknown title)" in records[0].message

    def test_no_secret_is_logged(
        self, build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        epub = build_epub([("Chapter One", "Test content")])
        item = _make_item(epub)

        with caplog.at_level(logging.DEBUG):
            plugin = EpubNormalizePlugin()
            plugin.process((item,), FakeContext(settings={"strip_fonts": True}))

        for record in caplog.records:
            # Assert no record contains "settings{" pattern
            msg = record.getMessage()
            assert "settings{" not in msg.lower()


# ---------------------------------------------------------------------------
# UI trigger test
# ---------------------------------------------------------------------------


class TestUiTriggers:
    def test_the_normalize_trigger_names_what_it_removes(self) -> None:
        """The Normalize action names what it does and describes the outcome."""
        manifest = EpubNormalizePlugin.manifest
        trigger = next(
            (t for t in manifest.ui_triggers if t.scope == "book_selection_action"), None
        )
        assert trigger is not None, "EPUB Normalize plugin has no book_selection_action trigger"
        assert trigger.label == "Normalize styling"
        assert (
            trigger.description
            == "Remove publisher CSS that overrides your reader's font, theme and text size."
        )
