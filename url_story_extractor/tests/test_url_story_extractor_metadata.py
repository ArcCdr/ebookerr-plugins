"""Tests for metadata enrichment in UrlStoryExtractorPlugin."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from ebookerr_sdk.spi import (
    InvocationMode,
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


class TestEnrichesANewStory:
    """Test enrichment of a single new story."""

    def test_enriches_a_new_story(self) -> None:
        """Assert a new story is enriched with metadata fields."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s/a-tale"]},
            metadata_by_url={
                "https://a.test/s/a-tale": {
                    "title": "The Real Title",
                    "author": "Jane",
                    "numChapters": "7",
                    "numWords": "12000",
                    "status": "Completed",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        patch = patches[0]
        assert patch.title == "The Real Title"
        assert patch.author == "Jane"
        assert patch.num_chapters == 7
        assert patch.num_words == 12000
        assert patch.status == "Completed"


class TestEnrichmentParsedDates:
    """Test date parsing during enrichment."""

    def test_enrichment_parses_dates(self) -> None:
        """Assert datePublished is parsed into a timezone-aware datetime."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1"]},
            metadata_by_url={"https://a.test/s1": {"datePublished": "2026-01-02 03:04:05"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        patch = patches[0]
        assert patch.date_published is not None
        assert str(patch.date_published) == "2026-01-02 03:04:05+00:00"


class TestEnrichmentDropsUnparseableDate:
    """Test handling of unparseable dates."""

    def test_enrichment_drops_an_unparseable_date(self) -> None:
        """Assert unparseable dates are dropped with no exception."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1"]},
            metadata_by_url={"https://a.test/s1": {"dateUpdated": "not a date"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        patch = patches[0]
        assert patch.date_updated is None


class TestEnrichmentDropsNonNumericChapterCount:
    """Test handling of non-numeric chapter counts."""

    def test_enrichment_drops_a_non_numeric_chapter_count(self) -> None:
        """Assert non-numeric chapter counts are dropped."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1"]},
            metadata_by_url={"https://a.test/s1": {"numChapters": "many"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        patch = patches[0]
        assert patch.num_chapters is None


class TestEnrichmentMapsGenreToTags:
    """Test genre to tags mapping."""

    def test_enrichment_maps_genre_to_tags(self) -> None:
        """Assert genre field is mapped to tags."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1"]},
            metadata_by_url={"https://a.test/s1": {"genre": "Romance, Drama"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        patch = patches[0]
        assert patch.tags == "Romance, Drama"


class TestEnrichmentNeverBlanksSlugTitle:
    """Test that slug title is preserved when metadata title is empty."""

    def test_enrichment_never_blanks_the_slug_title(self) -> None:
        """Assert empty metadata title does not overwrite slug-derived title."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/the-original-title"]},
            metadata_by_url={"https://x.test/s/the-original-title": {"title": ""}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        patch = patches[0]
        assert patch.title == "The Original Title"


class TestEnrichmentNeverBlanksListingAuthor:
    """Test that listing-derived author is preserved when metadata author is missing."""

    def test_enrichment_never_blanks_the_listing_author(self) -> None:
        """Assert missing metadata author does not overwrite listing-derived author."""
        pages = FakePages(
            urls_by_listing={
                "https://www.literotica.com/authors/jane-doe/works/stories": ["https://a.test/s1"]
            },
            metadata_by_url={"https://a.test/s1": {}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(
            "https://www.literotica.com/authors/jane-doe/works/stories", ctx
        )

        assert len(patches) == 1
        patch = patches[0]
        assert patch.author == "Jane Doe"


class TestKnownUrlsAreNotFetched:
    """Test that known URLs are not fetched."""

    def test_known_urls_are_not_fetched(self) -> None:
        """Assert URLs in known_urls are not fetched."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1", "https://b.test/s2"]},
            metadata_by_url={"https://b.test/s2": {"title": "New Story"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750},
            ui_context={"known_urls": "https://a.test/s1"},
        )

        patches = plugin.extract_stories("https://x.test/l", ctx)

        # Should have two patches, but only one fetch
        assert len(patches) == 2
        assert len(pages.metadata_calls) == 1
        assert pages.metadata_calls[0] == "https://b.test/s2"


class TestMissingKnownUrlsKeyEnrichesEverything:
    """Test that missing known_urls key enriches all stories."""

    def test_missing_known_urls_key_enriches_everything(self) -> None:
        """Assert all URLs are enriched when known_urls is missing."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1", "https://b.test/s2"]},
            metadata_by_url={
                "https://a.test/s1": {"title": "Story A"},
                "https://b.test/s2": {"title": "Story B"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750},
            ui_context={},
        )

        patches = plugin.extract_stories("https://x.test/l", ctx)

        # Both should be fetched
        assert len(patches) == 2
        assert len(pages.metadata_calls) == 2


class TestCapLimitsTheFetchCount:
    """Test that cap limits fetch count."""

    def test_cap_limits_the_fetch_count(self) -> None:
        """Assert only cap number of stories are fetched."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s1",
                    "https://b.test/s2",
                    "https://c.test/s3",
                    "https://d.test/s4",
                    "https://e.test/s5",
                ]
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "Story A"},
                "https://b.test/s2": {"title": "Story B"},
                "https://c.test/s3": {"title": "Story C"},
                "https://d.test/s4": {"title": "Story D"},
                "https://e.test/s5": {"title": "Story E"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 2, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        # All five patches returned, but only two fetched
        assert len(patches) == 5
        assert len(pages.metadata_calls) == 2
        # First two should have enriched titles, last three should be URL-only
        assert patches[0].title == "Story A"
        assert patches[1].title == "Story B"
        # patches[2], [3], [4] keep their URL-derived titles


class TestCapOfZeroFetchesNothing:
    """Test that cap of zero fetches nothing."""

    def test_cap_of_zero_fetches_nothing(self) -> None:
        """Assert cap=0 prevents all fetches."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s1",
                    "https://b.test/s2",
                    "https://c.test/s3",
                    "https://d.test/s4",
                    "https://e.test/s5",
                ]
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "Story A"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        # Five patches, zero fetches
        assert len(patches) == 5
        assert len(pages.metadata_calls) == 0


class TestDelayIsAppliedBetweenFetches:
    """Test that delay is applied between fetches."""

    @patch("time.sleep")
    def test_delay_is_applied_between_fetches(self, mock_sleep: MagicMock) -> None:
        """Assert sleep is called between fetches with correct delay."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s1",
                    "https://b.test/s2",
                    "https://c.test/s3",
                ]
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "Story A"},
                "https://b.test/s2": {"title": "Story B"},
                "https://c.test/s3": {"title": "Story C"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        plugin.extract_stories("https://x.test/l", ctx)

        # Should sleep twice (between 3 fetches)
        assert mock_sleep.call_count == 2
        # Each sleep call should be 0.75 seconds
        for call in mock_sleep.call_args_list:
            assert call[0][0] == 0.75


class TestNoDelayForASingleFetch:
    """Test that no delay occurs for a single fetch."""

    @patch("time.sleep")
    def test_no_delay_for_a_single_fetch(self, mock_sleep: MagicMock) -> None:
        """Assert sleep is never called for a single fetch."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1"]},
            metadata_by_url={"https://a.test/s1": {"title": "Story A"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        plugin.extract_stories("https://x.test/l", ctx)

        # No sleeps for single fetch
        assert mock_sleep.call_count == 0


class TestCancellationStopsBetweenFetches:
    """Test that cancellation stops between fetches."""

    def test_cancellation_stops_between_fetches(self) -> None:
        """Assert cancellation stops after first fetch."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s1",
                    "https://b.test/s2",
                    "https://c.test/s3",
                ]
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "Story A"},
                "https://b.test/s2": {"title": "Story B"},
                "https://c.test/s3": {"title": "Story C"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        # Cancel on second check_cancelled call (after first fetch)
        call_count = [0]

        def cancel_after_first_fetch() -> None:
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Cancelled")

        ctx.check_cancelled = cancel_after_first_fetch

        with pytest.raises(Exception, match="Cancelled"):
            plugin.extract_stories("https://x.test/l", ctx)

        # Only one fetch before cancellation
        assert len(pages.metadata_calls) == 1


class TestFailingFetchKeepsTheUrlRow:
    """Test that failing fetches keep the URL-derived row."""

    def test_a_failing_fetch_keeps_the_url_row(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert None metadata keeps the original patch unchanged."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1"]},
            metadata_by_url={"https://a.test/s1": None},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        with caplog.at_level(logging.DEBUG):
            patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        patch = patches[0]
        # Should keep slug title
        assert patch.url == "https://a.test/s1"
        assert "No metadata for" in caplog.text


class TestEnrichmentLogsTheBudget:
    """Test logging of enrichment budget."""

    def test_enrichment_logs_the_budget(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert INFO logs contain budget details."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://a.test/s1",
                    "https://b.test/s2",
                    "https://c.test/s3",
                ]
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "Story A"},
                "https://b.test/s2": {"title": "Story B"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 2, "request_delay_ms": 750})

        with caplog.at_level(logging.INFO):
            plugin.extract_stories("https://x.test/l", ctx)

        # Check for budget log
        budget_logs = [
            r
            for r in caplog.records
            if "Enriching 3 new story(ies) (cap 2, 750ms apart)" in r.message
        ]
        assert len(budget_logs) > 0

        # Check for result log
        result_logs = [
            r
            for r in caplog.records
            if "Enriched 2 of 3 new story(ies); 1 left for the next scan" in r.message
        ]
        assert len(result_logs) > 0


class TestNothingToEnrichLogsAtDebug:
    """Test that nothing to enrich is logged at debug level."""

    def test_nothing_to_enrich_logs_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert DEBUG log when all URLs are known."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1", "https://b.test/s2"]},
            metadata_by_url={"https://a.test/s1": {"title": "Story A"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750},
            ui_context={"known_urls": "https://a.test/s1\nhttps://b.test/s2"},
        )

        with caplog.at_level(logging.DEBUG):
            plugin.extract_stories("https://x.test/l", ctx)

        # No fetches should happen
        assert len(pages.metadata_calls) == 0
        # Should log nothing to enrich
        debug_logs = [r for r in caplog.records if "Nothing to enrich" in r.message]
        assert len(debug_logs) > 0


class TestScanEnrichesToo:
    """Test that scan enriches via the same pass."""

    def test_scan_enriches_too(self) -> None:
        """Assert scan calls enrichment on the patches it returns."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1"]},
            metadata_by_url={"https://a.test/s1": {"title": "Enriched Title"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://x.test/l"],
                "max_new_metadata_per_scan": 25,
                "request_delay_ms": 750,
            }
        )

        patches = plugin.scan(ctx)

        # Should have enriched the patch
        assert len(patches) == 1
        assert patches[0].title == "Enriched Title"


class TestTheCapIsSharedAcrossWatchedUrls:
    """Test that metadata cap is shared across all watched URLs."""

    def test_the_cap_is_shared_across_watched_urls(self) -> None:
        """Assert cap applies to entire scan, not per-listing."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": [
                    "https://a.test/s1",
                    "https://a.test/s2",
                    "https://a.test/s3",
                ],
                "https://b.test/l": [
                    "https://b.test/s1",
                    "https://b.test/s2",
                    "https://b.test/s3",
                ],
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "A1"},
                "https://a.test/s2": {"title": "A2"},
                "https://a.test/s3": {"title": "A3"},
                "https://b.test/s1": {"title": "B1"},
                "https://b.test/s2": {"title": "B2"},
                "https://b.test/s3": {"title": "B3"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://a.test/l", "https://b.test/l"],
                "max_new_metadata_per_scan": 4,
                "request_delay_ms": 0,
            }
        )

        plugin.scan(ctx)

        # Should fetch exactly 4, not 6 (not 3 per listing)
        assert len(pages.metadata_calls) == 4


class TestAStoryInTwoListingsIsFetchedOnce:
    """Test that stories in multiple listings are fetched only once."""

    def test_a_story_in_two_listings_is_fetched_once(self) -> None:
        """Assert a story in two listings is fetched once per scan."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://shared.test/s1"],
                "https://b.test/l": ["https://shared.test/s1"],
            },
            metadata_by_url={"https://shared.test/s1": {"title": "Shared Story"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://a.test/l", "https://b.test/l"],
                "max_new_metadata_per_scan": 10,
                "request_delay_ms": 0,
            }
        )

        patches = plugin.scan(ctx)

        # One patch returned despite being in two listings
        assert len(patches) == 1
        assert patches[0].url == "https://shared.test/s1"
        # One fetch for the shared URL
        assert len(pages.metadata_calls) == 1


class TestTheDelayIsSpacedAcrossListings:
    """Test that delay is shared across all listings in a scan."""

    @patch("time.sleep")
    def test_the_delay_is_spaced_across_listings(self, mock_sleep: MagicMock) -> None:
        """Assert delay applies across listings in the scan."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://a.test/s1"],
                "https://b.test/l": ["https://b.test/s1"],
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "A1"},
                "https://b.test/s1": {"title": "B1"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://a.test/l", "https://b.test/l"],
                "max_new_metadata_per_scan": 10,
                "request_delay_ms": 500,
            }
        )

        plugin.scan(ctx)

        # First fetch unspaced, second spaced: one sleep call
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args_list[0][0][0] == 0.5


class TestNoDelayForASingleFetchInAWholeScan:
    """Test that a single fetch across all listings has no delay."""

    @patch("time.sleep")
    def test_no_delay_for_a_single_fetch_in_a_whole_scan(self, mock_sleep: MagicMock) -> None:
        """Assert no delay when only one fetch happens in entire scan."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://a.test/s1"],
                "https://b.test/l": [],
            },
            metadata_by_url={"https://a.test/s1": {"title": "A1"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://a.test/l", "https://b.test/l"],
                "max_new_metadata_per_scan": 10,
                "request_delay_ms": 500,
            }
        )

        plugin.scan(ctx)

        # No sleeps for single fetch
        assert mock_sleep.call_count == 0


class TestScanLogsTheUnspentBudget:
    """Test that scan logs the unspent budget."""

    def test_scan_logs_the_unspent_budget(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert scan finish log includes unspent budget."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://a.test/s1"],
                "https://b.test/l": ["https://b.test/s1"],
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "A1"},
                "https://b.test/s1": {"title": "B1"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://a.test/l", "https://b.test/l"],
                "max_new_metadata_per_scan": 10,
                "request_delay_ms": 0,
            }
        )

        with caplog.at_level(logging.INFO):
            plugin.scan(ctx)

        # Check for the unspent budget in the finish log
        finish_log = "Scan finished: 2 story(ies) from 2 URL(s), 8 metadata fetch(es) unspent"
        assert finish_log in caplog.text


class TestKnownUrlsAreNotFetchedOnTheScanPath:
    """Test that known URLs are skipped on the scan path."""

    def test_known_urls_are_not_fetched_on_the_scan_path(self) -> None:
        """Assert known URLs are not fetched during scan."""
        pages = FakePages(
            urls_by_listing={"https://a.test/l": ["https://a.test/s1", "https://a.test/s2"]},
            metadata_by_url={"https://a.test/s2": {"title": "S2"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://a.test/l"],
                "max_new_metadata_per_scan": 10,
                "request_delay_ms": 0,
            },
            ui_context={"known_urls": "https://a.test/s1"},
        )

        patches = plugin.scan(ctx)

        # Two patches returned
        assert len(patches) == 2
        # Only one fetch (s2)
        assert len(pages.metadata_calls) == 1
        assert pages.metadata_calls[0] == "https://a.test/s2"


class TestExtractStoriesGetsItsOwnBudget:
    """Test that extract_stories gets its own budget."""

    def test_extract_stories_gets_its_own_budget(self) -> None:
        """Assert extract_stories does not share budget between calls."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://a.test/s1", "https://a.test/s2", "https://a.test/s3"]
            },
            metadata_by_url={
                "https://a.test/s1": {"title": "S1"},
                "https://a.test/s2": {"title": "S2"},
                "https://a.test/s3": {"title": "S3"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "max_new_metadata_per_scan": 2,
                "request_delay_ms": 0,
            }
        )

        # Call twice
        plugin.extract_stories("https://a.test/l", ctx)
        plugin.extract_stories("https://a.test/l", ctx)

        # 2 fetches per call = 4 total
        assert len(pages.metadata_calls) == 4


class TestKnownUrlsUseTheSpiDecoder:
    """Test that known_urls uses the SPI decoder."""

    def test_known_urls_use_the_spi_decoder(self) -> None:
        """Assert known_urls with padding is decoded correctly."""
        pages = FakePages(
            urls_by_listing={"https://a.test/l": ["https://a.test/s1"]},
            metadata_by_url={},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "max_new_metadata_per_scan": 25,
                "request_delay_ms": 0,
            },
            ui_context={"known_urls": " \nhttps://a.test/s1\n\n"},
        )

        plugin.extract_stories("https://a.test/l", ctx)

        # No fetch because the padded URL is recognized
        assert len(pages.metadata_calls) == 0


class TestAFailingFetchStillConsumesBudget:
    """Test that failed fetches consume budget."""

    def test_a_failing_fetch_still_consumes_budget(self) -> None:
        """Assert failed fetch consumes budget allowance."""
        pages = FakePages(
            urls_by_listing={"https://a.test/l": ["https://a.test/s1", "https://a.test/s2"]},
            metadata_by_url={
                "https://a.test/s1": None,  # Fails
                "https://a.test/s2": {"title": "S2"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "max_new_metadata_per_scan": 1,
                "request_delay_ms": 0,
            }
        )

        plugin.extract_stories("https://a.test/l", ctx)

        # Only one fetch total (budget limit), which happened to fail
        assert len(pages.metadata_calls) == 1


class TestEnrichmentUnescapesHtmlEntities:
    """Test HTML entity unescaping in fetched metadata."""

    def test_enrichment_unescapes_html_entities_in_category(self) -> None:
        """Assert HTML entities in category are decoded."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={
                "https://x.test/s/a": {"category": "Erotic Horror, Exhibitionist &amp; Voyeur"}
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].category == "Erotic Horror, Exhibitionist & Voyeur"

    def test_enrichment_unescapes_html_entities_in_the_title(self) -> None:
        """Assert HTML entities in title are decoded."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={"https://x.test/s/a": {"title": "Beauty &amp; the Beast"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].title == "Beauty & the Beast"

    def test_enrichment_unescapes_a_numeric_entity(self) -> None:
        """Assert numeric HTML entities are decoded."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={"https://x.test/s/a": {"author": "Jane&#39;s Ghost"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].author == "Jane's Ghost"


class TestEnrichmentParsesGroupedDigits:
    """Test parsing of digit-grouped counts."""

    def test_enrichment_parses_a_grouped_word_count(self) -> None:
        """Assert comma-grouped counts are parsed correctly."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={"https://x.test/s/a": {"numWords": "1,260,944"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].num_words == 1260944

    def test_enrichment_parses_a_space_grouped_word_count(self) -> None:
        """Assert space-grouped counts are parsed correctly."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={"https://x.test/s/a": {"numWords": "1 260 944"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].num_words == 1260944

    def test_enrichment_parses_a_plain_word_count(self) -> None:
        """Assert plain (ungrouped) counts are parsed correctly."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={"https://x.test/s/a": {"numWords": "12000"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].num_words == 12000


class TestEnrichmentHandlesEmptyParsedValues:
    """Test handling of empty values after parsing."""

    def test_enrichment_still_drops_a_non_numeric_count(self) -> None:
        """Assert non-numeric counts are dropped."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={"https://x.test/s/a": {"numChapters": "many"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].num_chapters is None

    def test_enrichment_ignores_an_entity_only_value(self) -> None:
        """Assert entity-only values that unescape to empty are dropped."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a"]},
            metadata_by_url={"https://x.test/s/a": {"status": "&nbsp;"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].status is None


class TestPartMetadataIsNotAppliedToTheRow:
    """Test that a parent work's metadata never overwrites a chapter row."""

    _LISTING = "https://www.literotica.com/authors/Morgan_Ellis/works/stories"
    _CHAPTER = "https://www.literotica.com/s/joan-of-snark-ch-02"
    _SERIES_META = {
        "storyUrl": "https://www.literotica.com/series/se/133778587",
        "title": "Joan of Snark",
        "author": "Morgan_Ellis",
        "authorUrl": "https://www.literotica.com/authors/Morgan_Ellis/works/stories",
        "category": "Erotic Horror",
        "numChapters": "17",
        "numWords": "90000",
        "status": "In-Progress",
        "description": "A series blurb.",
        "datePublished": "2019-10-15",
        "dateUpdated": "2023-06-09",
    }

    def test_a_part_keeps_its_url_derived_title(self) -> None:
        """Assert the chapter keeps its title, not the series title."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].title == "Joan Of Snark Ch. 02"

    def test_a_part_records_the_parent_as_its_series(self) -> None:
        """Assert the parent work is recorded as the series."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].series == "Joan of Snark"

    def test_a_part_records_the_parent_url_as_its_series_url(self) -> None:
        """Assert the parent URL is recorded as series_url."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].series_url == "https://www.literotica.com/series/se/133778587"

    def test_a_part_does_not_inherit_the_parent_chapter_count(self) -> None:
        """Assert the chapter count from parent metadata is not inherited."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].num_chapters is None

    def test_a_part_does_not_inherit_the_parent_word_count(self) -> None:
        """Assert the word count from parent metadata is not inherited."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].num_words is None

    def test_a_part_does_not_inherit_the_parent_dates(self) -> None:
        """Assert published and updated dates from parent are not inherited."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].date_published is None
        assert patches[0].date_updated is None

    def test_a_part_does_not_inherit_the_parent_status(self) -> None:
        """Assert status from parent metadata is not inherited."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].status is None

    def test_a_part_does_not_inherit_the_parent_description(self) -> None:
        """Assert description from parent metadata is not inherited."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].description is None

    def test_a_part_takes_the_author_from_the_parent(self) -> None:
        """Assert author and author_url from parent metadata are inherited."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].author == "Morgan_Ellis"
        assert (
            patches[0].author_url == "https://www.literotica.com/authors/Morgan_Ellis/works/stories"
        )

    def test_a_part_takes_the_category_from_the_parent(self) -> None:
        """Assert category from parent metadata is inherited."""
        pages = FakePages(
            urls_by_listing={self._LISTING: [self._CHAPTER]},
            metadata_by_url={self._CHAPTER: self._SERIES_META},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].category == "Erotic Horror"


class TestMetadataFetchedIsRecorded:
    """Test that metadata_fetched flag is recorded correctly."""

    def test_a_fetched_story_is_marked_fetched(self) -> None:
        """Assert a story with metadata is marked metadata_fetched=True."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={"https://x.test/s/a-tale": {"title": "A Tale"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].metadata_fetched is True

    def test_a_story_with_no_metadata_is_not_marked_fetched(self) -> None:
        """Assert a story with no metadata is marked metadata_fetched=False."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].metadata_fetched is False

    def test_an_empty_metadata_dict_still_counts_as_fetched(self) -> None:
        """Assert an empty metadata dict is still marked metadata_fetched=True."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={"https://x.test/s/a-tale": {}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].metadata_fetched is True

    def test_a_story_over_the_budget_is_not_marked_fetched(self) -> None:
        """Assert stories over budget are not marked metadata_fetched."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://x.test/s/a-tale",
                    "https://x.test/s/b-tale",
                    "https://x.test/s/c-tale",
                ]
            },
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "T"},
                "https://x.test/s/b-tale": {"title": "T"},
                "https://x.test/s/c-tale": {"title": "T"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 2, "request_delay_ms": 0})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 3
        # First two should be fetched, third should not
        assert patches[0].metadata_fetched is True
        assert patches[1].metadata_fetched is True
        assert patches[2].metadata_fetched is False

    def test_a_known_url_is_not_marked_fetched(self) -> None:
        """Assert known URLs are not marked metadata_fetched."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={"https://x.test/s/a-tale": {"title": "A Tale"}},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750},
            ui_context={"known_urls": "https://x.test/s/a-tale"},
        )

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].metadata_fetched is False

    def test_enrichment_shape_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert enrichment shape is logged when metadata is fetched."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://www.literotica.com/s/joan-of-snark-ch-02",
                    "https://x.test/s/a-tale",
                ]
            },
            metadata_by_url={
                "https://www.literotica.com/s/joan-of-snark-ch-02": {
                    "storyUrl": "https://www.literotica.com/series/se/133778587",
                    "title": "Joan of Snark",
                    "numChapters": "17",
                },
                "https://x.test/s/a-tale": {"title": "A Tale"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        with caplog.at_level(logging.INFO):
            plugin.extract_stories("https://x.test/l", ctx)

        assert "Enrichment shape: 1 part(s) of a larger work, 1 whole work(s)" in caplog.text

    def test_enrichment_shape_is_not_logged_when_nothing_was_fetched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert enrichment shape is not logged when no metadata is fetched."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        with caplog.at_level(logging.INFO):
            plugin.extract_stories("https://x.test/l", ctx)

        assert "Enrichment shape:" not in caplog.text

    def test_per_story_enrichment_is_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert per-story enrichment logs include part vs whole work classification."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/l": [
                    "https://www.literotica.com/s/joan-of-snark-ch-02",
                    "https://x.test/s/a-tale",
                ]
            },
            metadata_by_url={
                "https://www.literotica.com/s/joan-of-snark-ch-02": {
                    "storyUrl": "https://www.literotica.com/series/se/133778587",
                    "title": "Joan of Snark",
                    "numChapters": "17",
                },
                "https://x.test/s/a-tale": {"title": "A Tale"},
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 0})

        with caplog.at_level(logging.DEBUG):
            plugin.extract_stories("https://x.test/l", ctx)

        assert "as a part" in caplog.text
        assert 'series="Joan of Snark"' in caplog.text


class TestWholeWorkMetadataIsAppliedInFull:
    """Test that a whole work's metadata is applied fully."""

    _LISTING = "https://www.literotica.com/authors/Morgan_Ellis/works/stories"

    def test_a_whole_work_takes_the_metadata_title(self) -> None:
        """Assert whole work takes title from metadata."""
        pages = FakePages(
            urls_by_listing={
                self._LISTING: ["https://www.literotica.com/s/angelas-stepfather-ch-03"]
            },
            metadata_by_url={
                "https://www.literotica.com/s/angelas-stepfather-ch-03": {
                    "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                    "title": "Angela's Stepfather Ch. 03",
                    "author": "Morgan_Ellis",
                    "numChapters": "1",
                    "status": "Completed",
                    "datePublished": "2026-07-08",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].title == "Angela's Stepfather Ch. 03"

    def test_a_whole_work_takes_its_chapter_count(self) -> None:
        """Assert whole work takes chapter count from metadata."""
        pages = FakePages(
            urls_by_listing={
                self._LISTING: ["https://www.literotica.com/s/angelas-stepfather-ch-03"]
            },
            metadata_by_url={
                "https://www.literotica.com/s/angelas-stepfather-ch-03": {
                    "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                    "title": "Angela's Stepfather Ch. 03",
                    "author": "Morgan_Ellis",
                    "numChapters": "1",
                    "status": "Completed",
                    "datePublished": "2026-07-08",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].num_chapters == 1

    def test_a_whole_work_takes_its_status(self) -> None:
        """Assert whole work takes status from metadata."""
        pages = FakePages(
            urls_by_listing={
                self._LISTING: ["https://www.literotica.com/s/angelas-stepfather-ch-03"]
            },
            metadata_by_url={
                "https://www.literotica.com/s/angelas-stepfather-ch-03": {
                    "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                    "title": "Angela's Stepfather Ch. 03",
                    "author": "Morgan_Ellis",
                    "numChapters": "1",
                    "status": "Completed",
                    "datePublished": "2026-07-08",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].status == "Completed"

    def test_a_whole_work_takes_its_published_date(self) -> None:
        """Assert whole work takes published date from metadata."""
        pages = FakePages(
            urls_by_listing={
                self._LISTING: ["https://www.literotica.com/s/angelas-stepfather-ch-03"]
            },
            metadata_by_url={
                "https://www.literotica.com/s/angelas-stepfather-ch-03": {
                    "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                    "title": "Angela's Stepfather Ch. 03",
                    "author": "Morgan_Ellis",
                    "numChapters": "1",
                    "status": "Completed",
                    "datePublished": "2026-07-08",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].date_published is not None
        assert patches[0].date_published.year == 2026

    def test_a_whole_work_has_no_series_when_the_site_reports_none(self) -> None:
        """Assert whole work has no series when metadata does not provide one."""
        pages = FakePages(
            urls_by_listing={
                self._LISTING: ["https://www.literotica.com/s/angelas-stepfather-ch-03"]
            },
            metadata_by_url={
                "https://www.literotica.com/s/angelas-stepfather-ch-03": {
                    "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                    "title": "Angela's Stepfather Ch. 03",
                    "author": "Morgan_Ellis",
                    "numChapters": "1",
                    "status": "Completed",
                    "datePublished": "2026-07-08",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].series is None
        assert patches[0].series_url is None

    def test_a_series_url_row_keeps_its_metadata_title_and_count(self) -> None:
        """Assert a series URL row keeps its metadata title and chapter count."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://www.literotica.com/series/se/81737137"]},
            metadata_by_url={
                "https://www.literotica.com/series/se/81737137": {
                    "storyUrl": "https://www.literotica.com/series/se/81737137",
                    "title": "Olivia in Vulmonia",
                    "numChapters": "47",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].title == "Olivia in Vulmonia"
        assert patches[0].num_chapters == 47
        assert patches[0].series is None

    def test_a_whole_work_takes_the_reported_series_url(self) -> None:
        """Assert whole work takes seriesUrl from metadata."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={
                "https://x.test/s/a-tale": {
                    "storyUrl": "https://x.test/s/a-tale",
                    "title": "A Tale",
                    "series": "The Tales",
                    "seriesUrl": "https://x.test/series/9",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].series == "The Tales"
        assert patches[0].series_url == "https://x.test/series/9"


class TestListingFailureFailsTheScan:
    """Test that a listing failure propagates and fails the whole scan."""

    def test_a_listing_failure_fails_the_whole_scan(self) -> None:
        """Assert a ListingError from the gateway fails the entire scan."""
        from url_story_extractor.pages import ListingError

        class FailingPages(FakePages):
            """FakePages that raises ListingError for one URL."""

            def list_story_urls(self, url: str) -> list[str]:
                """Raise ListingError for the second URL."""
                if url == "https://x.test/b":
                    raise ListingError("could not list stories at https://x.test/b: boom")
                return super().list_story_urls(url)

        pages = FailingPages(
            urls_by_listing={
                "https://x.test/a": ["https://a.test/s1"],
                "https://x.test/b": ["https://b.test/s1"],
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://x.test/a", "https://x.test/b"],
                "max_new_metadata_per_scan": 0,
                "request_delay_ms": 0,
            }
        )

        with pytest.raises(ListingError):
            plugin.scan(ctx)

    def test_a_listing_failure_fails_the_extraction(self) -> None:
        """Assert a ListingError from the gateway fails extract_stories."""
        from url_story_extractor.pages import ListingError

        class FailingPages(FakePages):
            """FakePages that raises ListingError."""

            def list_story_urls(self, url: str) -> list[str]:
                """Raise ListingError."""
                raise ListingError("could not list stories at https://x.test/b: boom")

        pages = FailingPages()
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "max_new_metadata_per_scan": 0,
                "request_delay_ms": 0,
            }
        )

        with pytest.raises(ListingError):
            plugin.extract_stories("https://x.test/b", ctx)


class TestMetadataPassMapsTheSiteRating:
    """Test that the site rating (averrating) is mapped to story rating."""

    def test_metadata_pass_maps_the_site_rating(self) -> None:
        """Assert averrating is parsed as a float into the rating field."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "The Story", "averrating": "4.72"}
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].rating == 4.72

    def test_a_part_does_not_inherit_the_parent_rating(self) -> None:
        """Assert a chapter does not inherit rating from parent metadata."""
        listing_url = "https://www.literotica.com/authors/Morgan_Ellis/works/stories"
        chapter_url = "https://www.literotica.com/s/joan-of-snark-ch-02"
        series_meta = {
            "storyUrl": "https://www.literotica.com/series/se/133778587",
            "title": "Joan of Snark",
            "author": "Morgan_Ellis",
            "authorUrl": "https://www.literotica.com/authors/Morgan_Ellis/works/stories",
            "category": "Erotic Horror",
            "numChapters": "17",
            "averrating": "4.72",
        }
        pages = FakePages(
            urls_by_listing={listing_url: [chapter_url]},
            metadata_by_url={chapter_url: series_meta},
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories(listing_url, ctx)

        assert len(patches) == 1
        assert patches[0].rating is None

    def test_an_unparseable_rating_is_dropped(self) -> None:
        """Assert non-numeric ratings are dropped without raising."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/a-tale"]},
            metadata_by_url={
                "https://x.test/s/a-tale": {"title": "The Story", "averrating": "n/a"}
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 25, "request_delay_ms": 750})

        patches = plugin.extract_stories("https://x.test/l", ctx)

        assert len(patches) == 1
        assert patches[0].rating is None


class TestToFloatHelper:
    """Test the _to_float helper function."""

    def test_to_float_parses_strings_and_rejects_junk(self) -> None:
        """Assert _to_float converts numeric strings to float and rejects non-numeric."""
        from url_story_extractor.plugin import _to_float

        assert _to_float(" 4.5 ") == 4.5
        assert _to_float(None) is None
        assert _to_float("x") is None
        assert _to_float("4.72") == 4.72
        assert _to_float(4.72) == 4.72
