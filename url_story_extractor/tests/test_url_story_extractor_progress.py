"""Tests for progress reporting during metadata enrichment in UrlStoryExtractorPlugin."""

from __future__ import annotations

import logging
from typing import Any

from ebookerr_sdk.spi import (
    KNOWN_URLS_KEY,
    InvocationMode,
    PluginEventType,
    encode_known_urls,
)

from url_story_extractor.plugin import UrlStoryExtractorPlugin


class RecordingCtx:
    """PluginContext double that records every progress report."""

    def __init__(self, settings: dict[str, Any]) -> None:
        """Store settings and start an empty progress log.

        Args:
            settings: The plugin settings dict.
        """
        self.settings = settings
        self.logger = logging.getLogger("test")
        self.ui_context: dict[str, Any] = {}
        self.reports: list[float] = []
        self.mode = InvocationMode.HEADLESS
        self.event_type: PluginEventType | None = None
        self.cancelled = False

    def check_cancelled(self) -> None:
        """Never cancelled."""

    def report(self, percent: float) -> None:
        """Record one progress report.

        Args:
            percent: The progress percentage (0-100).
        """
        self.reports.append(percent)


class FakePages:
    """Test double for FanFicFarePagesGateway."""

    def __init__(
        self,
        urls_by_listing: dict[str, list[str]] | None = None,
        metadata_by_url: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        """Initialize with mappings of listing URLs to story URLs and metadata.

        Args:
            urls_by_listing: Maps listing URLs to lists of story URLs.
            metadata_by_url: Maps story URLs to metadata dicts or None.
        """
        self.urls_by_listing = urls_by_listing or {}
        self.metadata_by_url = metadata_by_url or {}

    def list_story_urls(self, url: str) -> list[str]:
        """Return the pre-configured URLs for this listing, or an empty list.

        Args:
            url: The listing URL.

        Returns:
            The list of story URLs for this listing.
        """
        return self.urls_by_listing.get(url, [])

    def fetch_story_metadata(self, url: str) -> dict[str, Any] | None:
        """Return pre-configured metadata for this URL, or None.

        Args:
            url: The story URL.

        Returns:
            The metadata dict or None.
        """
        return self.metadata_by_url.get(url)


class TestProgressReporting:
    """Test suite for progress reporting during metadata fetches."""

    def test_progress_reaches_one_hundred(self) -> None:
        """Assert final progress report is 100% when all fetches are done."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s/1",
                    "https://a.test/s/2",
                    "https://a.test/s/3",
                ]
            },
            metadata_by_url={
                "https://a.test/s/1": {"title": "Story 1"},
                "https://a.test/s/2": {"title": "Story 2"},
                "https://a.test/s/3": {"title": "Story 3"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = RecordingCtx(settings={"max_new_metadata_per_scan": 3, "request_delay_ms": 0})

        plugin.extract_stories("https://x.test/l", ctx)

        assert ctx.reports[-1] == 100.0

    def test_progress_is_reported_once_per_fetch(self) -> None:
        """Assert one progress report per story fetched."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s/1",
                    "https://a.test/s/2",
                    "https://a.test/s/3",
                ]
            },
            metadata_by_url={
                "https://a.test/s/1": {"title": "Story 1"},
                "https://a.test/s/2": {"title": "Story 2"},
                "https://a.test/s/3": {"title": "Story 3"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = RecordingCtx(settings={"max_new_metadata_per_scan": 3, "request_delay_ms": 0})

        plugin.extract_stories("https://x.test/l", ctx)

        assert len(ctx.reports) == 3

    def test_progress_is_monotonic(self) -> None:
        """Assert progress reports increase monotonically."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s/1",
                    "https://a.test/s/2",
                    "https://a.test/s/3",
                ]
            },
            metadata_by_url={
                "https://a.test/s/1": {"title": "Story 1"},
                "https://a.test/s/2": {"title": "Story 2"},
                "https://a.test/s/3": {"title": "Story 3"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = RecordingCtx(settings={"max_new_metadata_per_scan": 3, "request_delay_ms": 0})

        plugin.extract_stories("https://x.test/l", ctx)

        assert ctx.reports == sorted(ctx.reports)

    def test_progress_covers_only_the_fetched_slice(self) -> None:
        """Assert progress reports only fetched slice when budget is smaller than all."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s/1",
                    "https://a.test/s/2",
                    "https://a.test/s/3",
                    "https://a.test/s/4",
                ]
            },
            metadata_by_url={
                "https://a.test/s/1": {"title": "Story 1"},
                "https://a.test/s/2": {"title": "Story 2"},
                "https://a.test/s/3": {"title": "Story 3"},
                "https://a.test/s/4": {"title": "Story 4"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = RecordingCtx(settings={"max_new_metadata_per_scan": 2, "request_delay_ms": 0})

        plugin.extract_stories("https://x.test/l", ctx)

        assert ctx.reports == [50.0, 100.0]

    def test_a_failed_fetch_still_advances_progress(self) -> None:
        """Assert progress advances even when metadata fetch returns None."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s/1", "https://a.test/s/2"]},
            metadata_by_url={
                "https://a.test/s/1": None,
                "https://a.test/s/2": None,
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = RecordingCtx(settings={"max_new_metadata_per_scan": 2, "request_delay_ms": 0})

        plugin.extract_stories("https://x.test/l", ctx)

        assert ctx.reports == [50.0, 100.0]

    def test_no_progress_when_the_budget_is_zero(self) -> None:
        """Assert no progress reports when budget is zero."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s/1", "https://a.test/s/2"]},
            metadata_by_url={
                "https://a.test/s/1": {"title": "Story 1"},
                "https://a.test/s/2": {"title": "Story 2"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = RecordingCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        plugin.extract_stories("https://x.test/l", ctx)

        assert ctx.reports == []

    def test_no_progress_when_there_is_nothing_new(self) -> None:
        """Assert no progress reports when all stories are already known."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s/1"]},
            metadata_by_url={
                "https://a.test/s/1": {"title": "Story 1"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = RecordingCtx(settings={"max_new_metadata_per_scan": 1, "request_delay_ms": 0})
        ctx.ui_context[KNOWN_URLS_KEY] = encode_known_urls(["https://a.test/s/1"])

        plugin.extract_stories("https://x.test/l", ctx)

        assert ctx.reports == []
