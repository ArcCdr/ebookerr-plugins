"""Tests for metadata-only enrichment in UrlStoryExtractorPlugin."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from ebookerr_sdk.spi import (
    InvocationMode,
    MetadataEnrichingCatalog,
    PluginEventType,
)

from url_story_extractor.plugin import UrlStoryExtractorPlugin


class FakePages:
    """Test double for FanFicFarePagesGateway."""

    def __init__(
        self,
        urls_by_listing: dict[str, list[str]] | None = None,
        metadata_by_url: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        """Initialize with mappings of listing URLs to story URLs and metadata."""
        self.urls_by_listing = urls_by_listing or {}
        self.metadata_by_url = metadata_by_url or {}
        self.calls = []
        self.metadata_calls = []

    def list_story_urls(self, url: str) -> list[str]:
        """Return the pre-configured URLs for this listing, or an empty list."""
        self.calls.append(url)
        return self.urls_by_listing.get(url, [])

    def fetch_story_metadata(self, url: str) -> dict[str, Any] | None:
        """Return pre-configured metadata for this URL, or None."""
        self.metadata_calls.append(url)
        return self.metadata_by_url.get(url)


class FakeCtx:
    """Test double for PluginContext."""

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        logger: logging.Logger | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> None:
        """Initialize with settings, logger, and ui_context."""
        self.settings = settings or {}
        self.logger = logger or logging.getLogger("test")
        self.ui_context = ui_context or {}
        self.mode = InvocationMode.HEADLESS
        self.event_type: PluginEventType | None = None
        self.cancelled = False

    def check_cancelled(self) -> None:
        """Raise if cancellation was requested."""
        if self.cancelled:
            raise Exception("Cancelled")

    def report(self, percent: float) -> None:
        """Stub method."""
        pass


class FakeCtxWithReporting:
    """Test double for PluginContext that records report calls."""

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        logger: logging.Logger | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> None:
        """Initialize with settings, logger, and ui_context."""
        self.settings = settings or {}
        self.logger = logger or logging.getLogger("test")
        self.ui_context = ui_context or {}
        self.mode = InvocationMode.HEADLESS
        self.event_type: PluginEventType | None = None
        self.cancelled = False
        self.reported_values: list[float] = []

    def check_cancelled(self) -> None:
        """Raise if cancellation was requested."""
        if self.cancelled:
            raise Exception("Cancelled")

    def report(self, percent: float) -> None:
        """Record the reported value."""
        self.reported_values.append(percent)


class TestEnrichStoriesProtocol:
    """Test that enrich_stories implements MetadataEnrichingCatalog."""

    def test_enrich_stories_satisfies_the_protocol(self) -> None:
        """Assert UrlStoryExtractorPlugin implements MetadataEnrichingCatalog."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        assert isinstance(plugin, MetadataEnrichingCatalog)


class TestEnrichStoriesFunctionality:
    """Test the enrich_stories method."""

    def test_enrich_stories_returns_one_patch_per_fetched_url(self) -> None:
        """Assert each fetched URL produces one patch in order."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "A Tale", "status": "Completed"},
                "https://x.test/s/b-tale": {"title": "B Tale", "status": "Ongoing"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        result = plugin.enrich_stories(["https://x.test/s/a-tale", "https://x.test/s/b-tale"], ctx)

        assert len(result) == 2
        assert result[0].url == "https://x.test/s/a-tale"
        assert result[1].url == "https://x.test/s/b-tale"

    def test_enrich_stories_marks_every_returned_patch_fetched(self) -> None:
        """Assert all returned patches have metadata_fetched=True."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "A Tale"},
                "https://x.test/s/b-tale": {"title": "B Tale"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        result = plugin.enrich_stories(["https://x.test/s/a-tale", "https://x.test/s/b-tale"], ctx)

        for patch in result:
            assert patch.metadata_fetched is True

    def test_enrich_stories_omits_a_url_it_could_not_fetch(self) -> None:
        """Assert URLs without metadata are omitted from result."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "A Tale"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        result = plugin.enrich_stories(["https://x.test/s/a-tale", "https://x.test/s/b-tale"], ctx)

        assert len(result) == 1
        assert result[0].url == "https://x.test/s/a-tale"

    def test_enrich_stories_never_lists_a_page(self) -> None:
        """Assert no listing calls are made."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "A Tale"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        _ = plugin.enrich_stories(["https://x.test/s/a-tale"], ctx)

        assert pages.calls == []

    def test_enrich_stories_applies_the_metadata(self) -> None:
        """Assert fetched metadata fields are applied to patches."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a-tale": {
                    "title": "A Tale",
                    "status": "Completed",
                    "numChapters": "3",
                }
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        result = plugin.enrich_stories(["https://x.test/s/a-tale"], ctx)

        assert result[0].title == "A Tale"
        assert result[0].status == "Completed"
        assert result[0].num_chapters == 3

    def test_enrich_stories_honours_the_cap(self) -> None:
        """Assert metadata fetch cap is respected."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a": {"title": "A"},
                "https://x.test/s/b": {"title": "B"},
                "https://x.test/s/c": {"title": "C"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 2, "request_delay_ms": 0})

        result = plugin.enrich_stories(
            ["https://x.test/s/a", "https://x.test/s/b", "https://x.test/s/c"], ctx
        )

        assert len(result) == 2

    def test_enrich_stories_skips_an_unusable_url(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert unusable URLs are skipped and logged."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "A Tale"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        with caplog.at_level(logging.DEBUG):
            result = plugin.enrich_stories(["not a url", "https://x.test/s/a-tale"], ctx)

        assert len(result) == 1
        assert result[0].url == "https://x.test/s/a-tale"
        assert any("Skipped an unusable story URL" in record.message for record in caplog.records)

    def test_enrich_stories_of_an_empty_list_is_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert empty input returns empty output."""
        pages = FakePages()
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        with caplog.at_level(logging.INFO):
            result = plugin.enrich_stories([], ctx)

        assert result == []
        assert any(
            "Metadata pass skipped: no URL was requested" in record.message
            for record in caplog.records
        )

    def test_enrich_stories_logs_start_and_finish(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert start and finish messages are logged."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a": {"title": "A"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        with caplog.at_level(logging.INFO):
            _ = plugin.enrich_stories(["https://x.test/s/a", "https://x.test/s/b"], ctx)

        assert any(
            "Metadata pass started: 2 URL(s) requested, cap 25" in record.message
            for record in caplog.records
        )
        assert any(
            "Metadata pass finished: 1 of 2 URL(s) enriched" in record.message
            for record in caplog.records
        )

    def test_enrich_stories_derives_no_author_from_a_listing(self) -> None:
        """Assert author is None when no listing URL context."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "A"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        result = plugin.enrich_stories(["https://x.test/s/a-tale"], ctx)

        assert result[0].author is None

    def test_enrich_stories_reports_progress(self) -> None:
        """Assert progress is reported."""
        pages = FakePages(
            metadata_by_url={
                "https://x.test/s/a": {"title": "A"},
                "https://x.test/s/b": {"title": "B"},
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtxWithReporting(
            settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0}
        )

        _ = plugin.enrich_stories(["https://x.test/s/a", "https://x.test/s/b"], ctx)

        assert len(ctx.reported_values) > 0
        assert ctx.reported_values[-1] == 100.0
