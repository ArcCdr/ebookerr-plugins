"""Tests for the EpubValidatePlugin — manifest, settings, and per-book validation.

This test module verifies that the plugin correctly:
- Declares its manifest identity and capabilities.
- Handles settings schemas and default values.
- Validates EPUBs and writes findings as custom values.
- Manages report truncation at 32 KB.
- Logs findings at the correct severity levels.
- Forwards settings to the validate_epub service.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from ebookerr_sdk.spi import (
    BookView,
    EpubItem,
    ExternalLink,
    ExternalProgress,
    InvocationMode,
    PluginEventType,
    PluginType,
)
from ebookerr_sdk.testing import FakeContext
from ebookerr_sdk.validate.model import Finding, ValidationReport

_LOGGER = logging.getLogger("plugin.epub_validate")


def _item(
    epub_path: Path,
    *,
    book_id: str = "b1",
    title: str = "A Book",
) -> EpubItem:
    """Build a test EpubItem with a real BookView."""
    book = BookView(
        book_id=book_id,
        title=title,
        author="Test Author",
        story_url="https://example.com/story",
        output_filename="test.epub",
        num_chapters=5,
        status="In-Progress",
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
    )
    return EpubItem(book=book, epub_path=epub_path)


@pytest.fixture
def temp_epub(tmp_path: Path) -> Path:
    """Create a minimal EPUB file for testing."""
    epub_path = tmp_path / "test.epub"
    epub_path.write_bytes(b"minimal zip")
    return epub_path


class TestManifestIdentity:
    """Verify the plugin manifest identity and core fields."""

    def test_manifest_identity(self) -> None:
        """EpubValidatePlugin.manifest.id is 'epub_validate' with correct core fields."""
        from epub_validate.plugin import EpubValidatePlugin

        m = EpubValidatePlugin.manifest
        assert m.id == "epub_validate"
        assert m.plugin_type is PluginType.EPUB
        assert m.priority == 950
        assert m.headless is True
        assert m.headed is True
        assert m.accepts_list is True

    def test_manifest_events(self) -> None:
        """Manifest declares EPUB_CREATED and EPUB_MODIFIED events."""
        from epub_validate.plugin import EpubValidatePlugin

        m = EpubValidatePlugin.manifest
        assert set(m.events) == {PluginEventType.EPUB_CREATED, PluginEventType.EPUB_MODIFIED}

    def test_manifest_ui_trigger(self) -> None:
        """Manifest declares one UI trigger for book_selection_action."""
        from epub_validate.plugin import EpubValidatePlugin

        m = EpubValidatePlugin.manifest
        assert len(m.ui_triggers) == 1
        trigger = m.ui_triggers[0]
        assert trigger.scope == "book_selection_action"
        assert trigger.icon == "rule"
        assert trigger.label == "Validate"
        assert trigger.min_books == 1

    def test_manifest_declares_six_custom_values(self) -> None:
        """Manifest declares six custom values with correct types and display."""
        from epub_validate.plugin import EpubValidatePlugin

        m = EpubValidatePlugin.manifest
        assert len(m.custom_values) == 6
        decls = {d.key: d for d in m.custom_values}
        assert set(decls.keys()) == {
            "status",
            "checked_at",
            "error_count",
            "warning_count",
            "checker",
            "report",
        }
        # Check types
        assert decls["status"].type == "string"
        assert decls["checked_at"].type == "datetime"
        assert decls["error_count"].type == "int"
        assert decls["warning_count"].type == "int"
        assert decls["checker"].type == "string"
        assert decls["report"].type == "string"
        # Check all have display == "check_report"
        for decl in m.custom_values:
            assert decl.display == "check_report"

    def test_the_validate_trigger_keeps_its_label_and_gains_a_description(self) -> None:
        """The Validate action keeps its label and adds a description."""
        from epub_validate.plugin import EpubValidatePlugin

        manifest = EpubValidatePlugin.manifest
        trigger = next(
            (t for t in manifest.ui_triggers if t.scope == "book_selection_action"), None
        )
        assert trigger is not None, "EPUB Validate plugin has no book_selection_action trigger"
        assert trigger.label == "Validate"
        assert trigger.description == "Check the EPUB for structural errors and warnings."

    def test_epub_validate_status_is_filterable_and_aggregatable(self) -> None:
        """EpubValidatePlugin.manifest.custom_values has status with both flags True."""
        from epub_validate.plugin import EpubValidatePlugin

        manifest = EpubValidatePlugin.manifest
        status_decls = [d for d in manifest.custom_values if d.key == "status"]
        assert len(status_decls) == 1
        status_decl = status_decls[0]
        assert status_decl.filterable is True
        assert status_decl.aggregatable is True
        assert status_decl.label == "EPUB validation"

    def test_f8_the_validate_timeout_says_s(self) -> None:
        """F8: The external_timeout_s label ends with '(s)'."""
        from epub_validate.plugin import EpubValidatePlugin

        schema = EpubValidatePlugin.manifest.settings_schema
        field = next(f for f in schema.fields if f.key == "external_timeout_s")
        assert field.label == "External checker timeout (s)"

    def test_epub_validate_has_one_schema(self) -> None:
        """EPUB Validate plugin's settings_schema() returns the manifest schema."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        returned = plugin.settings_schema()
        declared = EpubValidatePlugin.manifest.settings_schema
        assert returned is declared

    def test_the_row_summary_template_names_the_built_in_checker(self) -> None:
        """The settings summary template names the built-in checker."""
        from epub_validate.plugin import EpubValidatePlugin

        manifest = EpubValidatePlugin.manifest
        assert manifest.settings_schema.summary == "{external_checker_command|Built-in checks only}"


class TestSettingsSchema:
    """Verify settings schema structure and defaults."""

    def test_settings_schema_fields(self) -> None:
        """Settings schema has exactly two fields with correct types and defaults."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        schema = plugin.settings_schema()
        assert len(schema.fields) == 2
        fields = {f.key: f for f in schema.fields}

        # Check external_checker_command
        assert "external_checker_command" in fields
        cmd_field = fields["external_checker_command"]
        assert cmd_field.type == "command"
        assert cmd_field.default == ""
        assert cmd_field.secret is False

        # Check external_timeout_s
        assert "external_timeout_s" in fields
        timeout_field = fields["external_timeout_s"]
        assert timeout_field.type == "int"
        assert timeout_field.default == 120
        assert timeout_field.secret is False


class TestProcessBasics:
    """Verify core process behavior: file preservation, patch structure."""

    def test_process_leaves_the_staged_file_byte_identical(self, temp_epub: Path) -> None:
        """process() preserves the staged EPUB file byte-for-byte (VAL-FR-8)."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        # Hash before
        hash_before = hashlib.sha256(temp_epub.read_bytes()).hexdigest()

        # Monkeypatch validate_epub to return a mock report
        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        # Hash after
        hash_after = hashlib.sha256(temp_epub.read_bytes()).hexdigest()
        assert hash_before == hash_after

    def test_process_returns_one_patch_per_item(self, temp_epub: Path) -> None:
        """process() returns one BookPatch per item in input order."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)

        # Create three items
        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
        )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            patches = plugin.process(items, ctx)

        assert len(patches) == 3
        assert patches[0].book_id == "b1"
        assert patches[1].book_id == "b2"
        assert patches[2].book_id == "b3"

    def test_patch_custom_values_match_the_contract(self, temp_epub: Path) -> None:
        """Patch carries exactly the six custom values with correct types and timestamps."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub, book_id="b1")

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.15,
            )
            patches = plugin.process((item,), ctx)

        result_patch = patches[0]
        assert result_patch.book_id == "b1"

        # Check keys
        assert set(result_patch.custom_values.keys()) == {
            "status",
            "checked_at",
            "error_count",
            "warning_count",
            "checker",
            "report",
        }

        # Check status
        assert result_patch.custom_values["status"].value == "valid"
        assert result_patch.custom_values["status"].value_type == "string"

        # Check error_count
        assert result_patch.custom_values["error_count"].value == 0
        assert result_patch.custom_values["error_count"].value_type == "int"

        # Check checker
        assert result_patch.custom_values["checker"].value == "native"
        assert result_patch.custom_values["checker"].value_type == "string"

        # Check checked_at is a valid datetime ISO string
        checked_at_value = result_patch.custom_values["checked_at"].value
        checked_at_dt = datetime.fromisoformat(checked_at_value)
        assert checked_at_dt is not None
        assert result_patch.custom_values["checked_at"].value_type == "datetime"

        # Check all updated_at timestamps match checked_at
        for write in result_patch.custom_values.values():
            assert write.updated_at == checked_at_value

    def test_process_writes_no_book_fields(self, temp_epub: Path) -> None:
        """Patch has no book fields, chapters, assets, delete, or emit_followup."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            patches = plugin.process((item,), ctx)

        result_patch = patches[0]
        assert result_patch.fields == {}
        assert result_patch.chapters == ()
        assert result_patch.assets == ()
        assert result_patch.delete is False
        assert result_patch.emit_followup is False


class TestReportJson:
    """Verify report JSON serialization and truncation."""

    def test_report_is_valid_json(self, temp_epub: Path) -> None:
        """Report custom value is valid JSON."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            patches = plugin.process((item,), ctx)

        report_value = patches[0].custom_values["report"].value
        parsed = json.loads(report_value)
        assert isinstance(parsed, dict)
        assert "findings" in parsed
        assert "fallback_reason" in parsed

    def test_report_is_an_empty_array_for_a_clean_book(self, temp_epub: Path) -> None:
        """Report has empty findings array for a book with no findings."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            patches = plugin.process((item,), ctx)

        report_value = patches[0].custom_values["report"].value
        parsed = json.loads(report_value)
        assert isinstance(parsed, dict)
        assert parsed.get("findings") == []
        assert parsed.get("fallback_reason") is None

    def test_report_entries_carry_the_four_keys(self, temp_epub: Path) -> None:
        """Report findings have exactly the four keys: code, severity, message, location."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        findings = (
            Finding(
                code="EBK-OPF-5",
                severity="error",
                message="Missing required element",
                location="content.opf",
            ),
        )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="errors",
                findings=findings,
                checker="native",
                error_count=1,
                warning_count=0,
                duration_s=0.1,
            )
            patches = plugin.process((item,), ctx)

        report_value = patches[0].custom_values["report"].value
        parsed = json.loads(report_value)
        assert isinstance(parsed, dict)
        assert "findings" in parsed
        assert len(parsed["findings"]) == 1
        entry = parsed["findings"][0]
        assert set(entry.keys()) == {"code", "severity", "message", "location"}
        assert entry["code"] == "EBK-OPF-5"
        assert entry["severity"] == "error"
        assert entry["message"] == "Missing required element"
        assert entry["location"] == "content.opf"

    def test_report_is_capped_at_32kb(self, temp_epub: Path) -> None:
        """Report is truncated to stay within 32 KB even with 500 large findings."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        # Create 500 findings with 200-char messages
        findings = tuple(
            Finding(
                code=f"EBK-TST-{i}",
                severity="warning",
                message="x" * 200,
                location="test.xhtml",
            )
            for i in range(500)
        )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="warnings",
                findings=findings,
                checker="native",
                error_count=0,
                warning_count=500,
                duration_s=0.1,
            )
            patches = plugin.process((item,), ctx)

        report_value = patches[0].custom_values["report"].value
        report_bytes = report_value.encode("utf-8")
        # The report object with findings array and fallback_reason should stay within limit
        assert len(report_bytes) <= 32768

    def test_report_json_keeps_whole_entries(self, temp_epub: Path) -> None:
        """Report findings are never truncated mid-object; all entries are complete."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        # Create 500 findings
        findings = tuple(
            Finding(
                code=f"EBK-TST-{i}",
                severity="warning",
                message="x" * 200,
                location="test.xhtml",
            )
            for i in range(500)
        )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="warnings",
                findings=findings,
                checker="native",
                error_count=0,
                warning_count=500,
                duration_s=0.1,
            )
            patches = plugin.process((item,), ctx)

        report_value = patches[0].custom_values["report"].value
        parsed = json.loads(report_value)
        assert isinstance(parsed, dict)
        # Every entry in findings must have all four keys
        for entry in parsed.get("findings", []):
            assert set(entry.keys()) == {"code", "severity", "message", "location"}


class TestProgress:
    """Verify progress reporting."""

    def test_process_reports_progress(self, temp_epub: Path) -> None:
        """process() reports progress reaching 100.0 with one entry per item."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        items = (
            _item(temp_epub, book_id="b1"),
            _item(temp_epub, book_id="b2"),
            _item(temp_epub, book_id="b3"),
        )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            plugin.process(items, ctx)

        assert len(ctx.reports) == 3
        assert ctx.reports[-1][0] == 100.0


class TestUnreadableArchive:
    """Verify handling of unreadable EPUB files."""

    def test_unreadable_archive_yields_status_unreadable(self, temp_epub: Path) -> None:
        """Unreadable file yields status='unreadable' without raising."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub, title="A Book", book_id="b1")

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="unreadable",
                findings=(
                    Finding(
                        code="EBK-PKG-1",
                        severity="error",
                        message="Not a valid zip archive",
                        location="",
                    ),
                ),
                checker="native",
                error_count=1,
                warning_count=0,
                duration_s=0.05,
            )
            patches = plugin.process((item,), ctx)

        assert len(patches) == 1
        assert patches[0].custom_values["status"].value == "unreadable"

    def test_unreadable_archive_logs_a_warning(self, temp_epub: Path, caplog) -> None:
        """Unreadable file triggers a WARNING-level log entry."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub, title="A Book", book_id="b1")

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="unreadable",
                findings=(
                    Finding(
                        code="EBK-PKG-1",
                        severity="error",
                        message="Not a valid zip archive",
                        location="",
                    ),
                ),
                checker="native",
                error_count=1,
                warning_count=0,
                duration_s=0.05,
            )
            with caplog.at_level(logging.WARNING, logger="plugin.epub_validate"):
                plugin.process((item,), ctx)

        # Check the warning was logged
        warning_found = any(
            'EPUB validation could not read the archive for "A Book" (book_id=b1)' in record.message
            for record in caplog.records
            if record.levelname == "WARNING"
        )
        assert warning_found


class TestLogging:
    """Verify logging of findings and validation results."""

    def test_info_line_names_the_title_status_and_counts(self, temp_epub: Path, caplog) -> None:
        """INFO log line includes title, status, error/warning counts, and checker."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub, title="A Book", book_id="b1")

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            with caplog.at_level(logging.INFO, logger="plugin.epub_validate"):
                plugin.process((item,), ctx)

        # Check the info log message
        info_found = any(
            'EPUB validation finished for "A Book" (book_id=b1): valid — 0 error(s), 0 warning(s)'
            in record.message
            and "(checker=native)" in record.message
            for record in caplog.records
            if record.levelname == "INFO"
        )
        assert info_found

    def test_debug_line_per_finding(self, temp_epub: Path, caplog) -> None:
        """DEBUG log line per finding includes code, severity, message, and location."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub, book_id="b1")

        findings = (
            Finding(
                code="EBK-OPF-5",
                severity="error",
                message="Missing dc:language",
                location="content.opf",
            ),
        )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="errors",
                findings=findings,
                checker="native",
                error_count=1,
                warning_count=0,
                duration_s=0.1,
            )
            with caplog.at_level(logging.DEBUG, logger="plugin.epub_validate"):
                plugin.process((item,), ctx)

        # Check for debug log
        debug_found = any(
            "EPUB validation finding: book_id=b1 EBK-OPF-5 [error]" in record.message
            for record in caplog.records
            if record.levelname == "DEBUG"
        )
        assert debug_found

    def test_logs_are_attributed_to_the_plugin_logger(self, temp_epub: Path, caplog) -> None:
        """Log records are attributed to 'plugin.epub_validate' logger."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            with caplog.at_level(logging.DEBUG, logger="plugin.epub_validate"):
                plugin.process((item,), ctx)

        assert any(record.name == "plugin.epub_validate" for record in caplog.records)


class TestSettingsForwarding:
    """Verify settings are correctly forwarded to validate_epub."""

    def test_external_command_setting_is_forwarded(self, temp_epub: Path) -> None:
        """External command and timeout settings are forwarded to validate_epub."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(
            settings={
                "external_checker_command": " /opt/epubcheck ",
                "external_timeout_s": 30,
            },
            logger=_LOGGER,
        )
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        # Check that validate_epub was called with the right arguments
        mock_validate.assert_called_once()
        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs["external_command"] == "/opt/epubcheck"
        assert call_kwargs["timeout_s"] == 30

    def test_a_blank_external_command_is_passed_as_none(self, temp_epub: Path) -> None:
        """Blank external command setting is passed as None."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(settings={"external_checker_command": "  "}, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs["external_command"] is None

    def test_a_bad_timeout_setting_falls_back_to_120(self, temp_epub: Path) -> None:
        """Non-integer timeout setting is coerced to 120."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(settings={"external_timeout_s": "nonsense"}, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs["timeout_s"] == 120

    def test_a_zero_timeout_setting_is_clamped_to_one(self, temp_epub: Path) -> None:
        """Zero timeout setting is clamped to 1."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(settings={"external_timeout_s": 0}, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs["timeout_s"] == 1


class TestFallbackMessages:
    """Verify that fallback reasons are included in result messages."""

    def test_result_message_names_the_fallback(self, temp_epub: Path, caplog) -> None:
        """Result message names the fallback reason and logs a WARNING."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)
        item = _item(temp_epub, book_id="b1")

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
                fallback_reason="not found",
            )
            with caplog.at_level(logging.WARNING, logger="plugin.epub_validate"):
                plugin.process((item,), ctx)

        # Check the message
        assert len(ctx.alerts) == 1
        message = ctx.alerts[0]
        assert "external checker not found" in message
        assert "used the built-in checker" in message

        # Check the warning log
        warning_found = any(
            "reason=not found" in record.message
            for record in caplog.records
            if (
                record.levelname == "WARNING"
                and "EPUB validation used the built-in checker" in record.message
            )
        )
        assert warning_found

    def test_result_message_is_unchanged_without_a_fallback(self, temp_epub: Path, caplog) -> None:
        """Result message without fallback is unchanged; no WARNING logged."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)
        item = _item(temp_epub, book_id="b1")

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
                fallback_reason=None,
            )
            with caplog.at_level(logging.WARNING, logger="plugin.epub_validate"):
                plugin.process((item,), ctx)

        # Check the message
        assert len(ctx.alerts) == 1
        message = ctx.alerts[0]
        assert message == "EPUB validation passed: valid"

        # Check no fallback warning was logged
        warning_found = any(
            "EPUB validation used the built-in checker" in record.message
            for record in caplog.records
            if record.levelname == "WARNING"
        )
        assert not warning_found

    def test_an_unknown_reason_falls_through_verbatim(self, temp_epub: Path) -> None:
        """Unknown fallback reason appears verbatim in the message."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)
        item = _item(temp_epub, book_id="b1")

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
                fallback_reason="something new",
            )
            plugin.process((item,), ctx)

        # Check the message
        assert len(ctx.alerts) == 1
        message = ctx.alerts[0]
        assert "something new" in message
