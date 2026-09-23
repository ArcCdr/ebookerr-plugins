"""Tests for EpubValidatePlugin summary messaging and exception isolation.

This test module verifies that the plugin correctly:
- Emits one summary per run via alert (HEADED) or notify (HEADLESS).
- Isolates exceptions without aborting the batch (VAL-FR-7).
- Counts failures correctly in the summary.
- Preserves files under exception (VAL-FR-8).
- Never both alert and notify in a single run.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from ebookerr_sdk.spi import (
    BookView,
    EpubItem,
    ExternalLink,
    ExternalProgress,
    InvocationMode,
)
from ebookerr_sdk.testing import Cancelled, FakeContext
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


class TestHeadlessSummary:
    """Verify HEADLESS mode summary behavior: notify only on errors."""

    def test_headless_clean_run_publishes_nothing(self, temp_epub: Path) -> None:
        """One valid book in HEADLESS → ctx.notices == [] and ctx.alerts == []."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)
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

        assert ctx.notices == []
        assert ctx.alerts == []

    def test_headless_warnings_only_publishes_one_warning_toast(self, temp_epub: Path) -> None:
        """Book with only warnings in HEADLESS → one transient warning toast."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="warnings",
                findings=(
                    Finding(
                        code="EBK-OPF-1",
                        severity="warning",
                        message='dc:language="English" is not recommended',
                        location="content.opf",
                    ),
                ),
                checker="native",
                error_count=0,
                warning_count=1,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        assert ctx.notices == [("warning", "EPUB validation passed: warnings", False)]
        assert ctx.alerts == []

    def test_a_headless_warnings_only_summary_is_transient(self, temp_epub: Path) -> None:
        """One warnings-only book in HEADLESS → transient notification (not durable)."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="warnings",
                findings=(
                    Finding(
                        code="EBK-OPF-1",
                        severity="warning",
                        message='dc:language="English" is not recommended',
                        location="content.opf",
                    ),
                ),
                checker="native",
                error_count=0,
                warning_count=1,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        assert ctx.notices == [("warning", "EPUB validation passed: warnings", False)]

    def test_headless_one_failing_book_publishes_one_warning_toast(self, temp_epub: Path) -> None:
        """Book with errors in HEADLESS → one warning toast."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="errors",
                findings=(
                    Finding(
                        code="EBK-OPF-5",
                        severity="error",
                        message="Missing dc:language",
                        location="content.opf",
                    ),
                ),
                checker="native",
                error_count=1,
                warning_count=0,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        assert ctx.notices == [
            ("warning", "EPUB validation found errors in 1 of 1 book(s)", True)
        ]
        assert ctx.alerts == []

    def test_headless_batch_publishes_exactly_one_aggregate_toast(self, temp_epub: Path) -> None:
        """Five items, two with errors in HEADLESS → one aggregate toast (§C.6)."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
            _item(temp_epub, book_id="b4", title="Book 4"),
            _item(temp_epub, book_id="b5", title="Book 5"),
        )

        call_count = [0]

        def mock_validate_side_effect(path, **kw):
            # Fail on items at indices 1 and 3 (second and fourth items)
            if call_count[0] in (1, 3):
                result = ValidationReport(
                    status="errors",
                    findings=(
                        Finding(
                            code="EBK-OPF-5",
                            severity="error",
                            message="Error",
                            location="content.opf",
                        ),
                    ),
                    checker="native",
                    error_count=1,
                    warning_count=0,
                    duration_s=0.1,
                )
            else:
                result = ValidationReport(
                    status="valid",
                    findings=(),
                    checker="native",
                    error_count=0,
                    warning_count=0,
                    duration_s=0.1,
                )
            call_count[0] += 1
            return result

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            plugin.process(items, ctx)

        assert len(ctx.notices) == 1
        assert ctx.notices[0] == (
            "warning",
            "EPUB validation found errors in 2 of 5 book(s)",
            True,
        )

    def test_headless_batch_with_no_errors_publishes_nothing(self, temp_epub: Path) -> None:
        """Five valid books in HEADLESS → ctx.notices == []."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)

        items = tuple(_item(temp_epub, book_id=f"b{i}", title=f"Book {i}") for i in range(5))

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

        assert ctx.notices == []

    def test_headless_unreadable_book_publishes_one_warning_toast(self, temp_epub: Path) -> None:
        """Unreadable file (status='unreadable') in HEADLESS → one warning toast."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="unreadable",
                findings=(
                    Finding(
                        code="EBK-PKG-1",
                        severity="error",
                        message="Not a valid zip",
                        location="",
                    ),
                ),
                checker="native",
                error_count=1,
                warning_count=0,
                duration_s=0.05,
            )
            plugin.process((item,), ctx)

        assert ctx.notices == [("warning", "EPUB validation passed: unreadable", True)]

    def test_headless_batch_with_warnings_toasts_the_count(self, temp_epub: Path) -> None:
        """Five items, two with warnings, HEADLESS → one toast with count."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
            _item(temp_epub, book_id="b4", title="Book 4"),
            _item(temp_epub, book_id="b5", title="Book 5"),
        )

        call_count = [0]

        def mock_validate_side_effect(path, **kw):
            # Warnings on items at indices 1 and 3 (second and fourth items)
            if call_count[0] in (1, 3):
                result = ValidationReport(
                    status="warnings",
                    findings=(
                        Finding(
                            code="EBK-OPF-1",
                            severity="warning",
                            message="Warning",
                            location="content.opf",
                        ),
                    ),
                    checker="native",
                    error_count=0,
                    warning_count=1,
                    duration_s=0.1,
                )
            else:
                result = ValidationReport(
                    status="valid",
                    findings=(),
                    checker="native",
                    error_count=0,
                    warning_count=0,
                    duration_s=0.1,
                )
            call_count[0] += 1
            return result

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            plugin.process(items, ctx)

        assert ctx.notices == [
            ("warning", "EPUB validation passed for all 5 book(s) — 2 with warnings", False)
        ]

    def test_a_batch_with_warnings_and_an_unreadable_book_stays_durable(
        self, temp_epub: Path
    ) -> None:
        """Five items: two warnings, one unreadable → one durable notification."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
            _item(temp_epub, book_id="b4", title="Book 4"),
            _item(temp_epub, book_id="b5", title="Book 5"),
        )

        call_count = [0]

        def mock_validate_side_effect(path, **kw):
            # Warnings on items at indices 0 and 2, unreadable on index 3
            if call_count[0] == 0 or call_count[0] == 2:
                result = ValidationReport(
                    status="warnings",
                    findings=(
                        Finding(
                            code="EBK-OPF-1",
                            severity="warning",
                            message="Warning",
                            location="content.opf",
                        ),
                    ),
                    checker="native",
                    error_count=0,
                    warning_count=1,
                    duration_s=0.1,
                )
            elif call_count[0] == 3:
                result = ValidationReport(
                    status="unreadable",
                    findings=(
                        Finding(
                            code="EBK-PKG-1",
                            severity="error",
                            message="Not a valid zip",
                            location="",
                        ),
                    ),
                    checker="native",
                    error_count=1,
                    warning_count=0,
                    duration_s=0.05,
                )
            else:
                result = ValidationReport(
                    status="valid",
                    findings=(),
                    checker="native",
                    error_count=0,
                    warning_count=0,
                    duration_s=0.1,
                )
            call_count[0] += 1
            return result

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            plugin.process(items, ctx)

        assert len(ctx.notices) == 1
        # The worst status is "unreadable" (comes before "warnings" alphabetically).
        # With an unreadable book present, the notification should be durable.
        assert ctx.notices[0][2] is True  # durable flag


class TestHeadedSummary:
    """Verify HEADED mode summary behavior: always alert, never notify."""

    def test_headed_clean_single_run_alerts_once(self, temp_epub: Path) -> None:
        """One valid book in HEADED → ctx.alerts == ['EPUB validation passed: valid']."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)
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

        assert ctx.alerts == ["EPUB validation passed: valid"]
        assert ctx.notices == []

    def test_headed_clean_batch_alerts_with_the_count(self, temp_epub: Path) -> None:
        """Three valid books in HEADED → alert with count."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)

        items = tuple(_item(temp_epub, book_id=f"b{i}", title=f"Book {i}") for i in range(3))

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

        assert ctx.alerts == ["EPUB validation passed for all 3 book(s)"]
        assert ctx.notices == []

    def test_headed_failing_run_alerts_and_does_not_toast(self, temp_epub: Path) -> None:
        """Two books, one failing, HEADED → alert only (VAL-D10)."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
        )

        call_count = [0]

        def mock_validate_side_effect(path, **kw):
            # Fail on the second item (index 1)
            if call_count[0] == 1:
                result = ValidationReport(
                    status="errors",
                    findings=(
                        Finding(
                            code="EBK-OPF-5",
                            severity="error",
                            message="Error",
                            location="content.opf",
                        ),
                    ),
                    checker="native",
                    error_count=1,
                    warning_count=0,
                    duration_s=0.1,
                )
            else:
                result = ValidationReport(
                    status="valid",
                    findings=(),
                    checker="native",
                    error_count=0,
                    warning_count=0,
                    duration_s=0.1,
                )
            call_count[0] += 1
            return result

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            plugin.process(items, ctx)

        assert ctx.alerts == ["EPUB validation found errors in 1 of 2 book(s)"]
        assert ctx.notices == []

    def test_headed_warnings_only_alert_names_the_status(self, temp_epub: Path) -> None:
        """One book with status='warnings', HEADED → alert names status."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.return_value = ValidationReport(
                status="warnings",
                findings=(
                    Finding(
                        code="EBK-OPF-1",
                        severity="warning",
                        message="Warning",
                        location="content.opf",
                    ),
                ),
                checker="native",
                error_count=0,
                warning_count=1,
                duration_s=0.1,
            )
            plugin.process((item,), ctx)

        assert ctx.alerts == ["EPUB validation passed: warnings"]

    def test_empty_selection_alerts_nothing_useful_headless(self, temp_epub: Path) -> None:
        """process((), ctx) in HEADLESS → returns [], no notifications."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)

        patches = plugin.process((), ctx)

        assert patches == []
        assert ctx.notices == []

    def test_empty_selection_headed_alerts_the_no_op(self, temp_epub: Path) -> None:
        """process((), ctx) in HEADED → alert with the no-op message."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)

        patches = plugin.process((), ctx)

        assert patches == []
        assert ctx.alerts == ["EPUB validation had nothing to check"]

    def test_headed_batch_with_warnings_alerts_the_count(self, temp_epub: Path) -> None:
        """Five items, two with warnings, HEADED → alert with count."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
            _item(temp_epub, book_id="b4", title="Book 4"),
            _item(temp_epub, book_id="b5", title="Book 5"),
        )

        call_count = [0]

        def mock_validate_side_effect(path, **kw):
            # Warnings on items at indices 1 and 3 (second and fourth items)
            if call_count[0] in (1, 3):
                result = ValidationReport(
                    status="warnings",
                    findings=(
                        Finding(
                            code="EBK-OPF-1",
                            severity="warning",
                            message="Warning",
                            location="content.opf",
                        ),
                    ),
                    checker="native",
                    error_count=0,
                    warning_count=1,
                    duration_s=0.1,
                )
            else:
                result = ValidationReport(
                    status="valid",
                    findings=(),
                    checker="native",
                    error_count=0,
                    warning_count=0,
                    duration_s=0.1,
                )
            call_count[0] += 1
            return result

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            plugin.process(items, ctx)

        assert ctx.alerts == ["EPUB validation passed for all 5 book(s) — 2 with warnings"]
        assert ctx.notices == []

    def test_a_run_logs_its_verdict_counts_at_info(self, temp_epub: Path, caplog) -> None:
        """Five items, HEADED → INFO log with verdict counts."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADED, logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
            _item(temp_epub, book_id="b4", title="Book 4"),
            _item(temp_epub, book_id="b5", title="Book 5"),
        )

        call_count = [0]

        def mock_validate_side_effect(path, **kw):
            # Warnings on items at indices 1 and 3 (second and fourth items)
            if call_count[0] in (1, 3):
                result = ValidationReport(
                    status="warnings",
                    findings=(
                        Finding(
                            code="EBK-OPF-1",
                            severity="warning",
                            message="Warning",
                            location="content.opf",
                        ),
                    ),
                    checker="native",
                    error_count=0,
                    warning_count=1,
                    duration_s=0.1,
                )
            else:
                result = ValidationReport(
                    status="valid",
                    findings=(),
                    checker="native",
                    error_count=0,
                    warning_count=0,
                    duration_s=0.1,
                )
            call_count[0] += 1
            return result

        with (
            patch("epub_validate.plugin.validate_epub") as mock_validate,
            caplog.at_level(logging.INFO, logger="epub_validate.plugin"),
        ):
            mock_validate.side_effect = mock_validate_side_effect
            plugin.process(items, ctx)

        # Check the INFO log contains the verdict counts
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "EPUB validation run finished: 5 book(s) — 3 valid, 2 with warnings, 0 with "
            "errors, 0 unreadable" in r.message
            for r in info_records
        )


class TestExceptionIsolation:
    """Verify exception isolation (VAL-FR-7): crashes don't abort the batch."""

    def test_an_unexpected_error_does_not_abort_the_batch(self, temp_epub: Path) -> None:
        """Second of three items raises; batch returns two patches."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
        )

        call_count = [0]

        def mock_validate_side_effect(path, **kw):
            # Raise on the second item (index 1)
            if call_count[0] == 1:
                call_count[0] += 1
                raise RuntimeError("boom")
            call_count[0] += 1
            return ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            patches = plugin.process(items, ctx)

        assert len(patches) == 2
        assert patches[0].book_id == "b1"
        assert patches[1].book_id == "b3"
        assert ctx.reports[-1][0] == 100.0

    def test_an_unexpected_error_is_logged_with_a_traceback(self, temp_epub: Path, caplog) -> None:
        """Second item raises; error is logged with traceback."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="A Book"),
            _item(temp_epub, book_id="b3", title="Book 3"),
        )

        def mock_validate_side_effect(path, **kw):
            if path == items[1].epub_path:
                raise RuntimeError("boom")
            return ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            with caplog.at_level(logging.ERROR, logger="plugin.epub_validate"):
                plugin.process(items, ctx)

        # Check the error was logged with a traceback
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            'EPUB validation failed unexpectedly for "A Book"' in r.message for r in error_records
        )
        assert any(r.exc_info for r in error_records)

    def test_an_unexpected_error_counts_toward_the_toast(self, temp_epub: Path) -> None:
        """One item, validate_epub raises, HEADLESS → warning toast."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(mode=InvocationMode.HEADLESS, logger=_LOGGER)
        item = _item(temp_epub)

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = RuntimeError("boom")
            plugin.process((item,), ctx)

        assert ctx.notices == [
            ("warning", "EPUB validation found errors in 1 of 1 book(s)", True)
        ]

    def test_a_cancellation_propagates(self, temp_epub: Path) -> None:
        """ctx.check_cancelled raises Cancelled → exception propagates."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(cancel_after=0, logger=_LOGGER)
        item = _item(temp_epub)

        with pytest.raises(Cancelled):
            plugin.process((item,), ctx)

        assert ctx.notices == []

    def test_process_still_leaves_the_file_byte_identical(self, temp_epub: Path) -> None:
        """Second item raises; files unchanged (VAL-FR-8)."""
        from epub_validate.plugin import EpubValidatePlugin

        plugin = EpubValidatePlugin()
        ctx = FakeContext(logger=_LOGGER)

        items = (
            _item(temp_epub, book_id="b1", title="Book 1"),
            _item(temp_epub, book_id="b2", title="Book 2"),
            _item(temp_epub, book_id="b3", title="Book 3"),
        )

        # Hash before
        hash_before = hashlib.sha256(temp_epub.read_bytes()).hexdigest()

        def mock_validate_side_effect(path, **kw):
            if path == items[1].epub_path:
                raise RuntimeError("boom")
            return ValidationReport(
                status="valid",
                findings=(),
                checker="native",
                error_count=0,
                warning_count=0,
                duration_s=0.1,
            )

        with patch("epub_validate.plugin.validate_epub") as mock_validate:
            mock_validate.side_effect = mock_validate_side_effect
            plugin.process(items, ctx)

        # Hash after
        hash_after = hashlib.sha256(temp_epub.read_bytes()).hexdigest()
        assert hash_before == hash_after
