"""Tests for the UrlStoryExtractorPlugin."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from ebookerr_sdk.spi import (
    InvocationMode,
    PluginEventType,
    PluginType,
)
from url_story_extractor.plugin import UrlStoryExtractorPlugin


class FakePages:
    """Test double for FanFicFarePagesGateway."""

    def __init__(self, urls_by_listing: dict[str, list[str]] | None = None) -> None:
        """Initialize with a mapping of listing URLs to story URLs."""
        self.urls_by_listing = urls_by_listing or {}
        self.calls = []
        self.metadata_calls = []

    def list_story_urls(self, url: str) -> list[str]:
        """Return the pre-configured URLs for this listing, or an empty list."""
        self.calls.append(url)
        return self.urls_by_listing.get(url, [])

    def fetch_story_metadata(self, url: str) -> dict[str, Any] | None:
        """Stub method (unused in this card)."""
        self.metadata_calls.append(url)
        return None


class FakeCtx:
    """Test double for PluginContext."""

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        logger: logging.Logger | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> None:
        """Initialize with settings and a logger."""
        self.settings = settings or {}
        self.logger = logger or logging.getLogger("test")
        self.ui_context = ui_context or {}
        self.mode = InvocationMode.HEADLESS
        self.event_type: PluginEventType | None = None

    def check_cancelled(self) -> None:
        """Stub method."""
        pass

    def report(self, percent: float) -> None:
        """Stub method."""
        pass


class TestManifest:
    """Test the plugin's manifest declaration."""

    def test_manifest_id_and_type(self) -> None:
        """Assert manifest declares correct id, type, priority, and network flag."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        assert plugin.manifest.id == "url_story_extractor"
        assert plugin.manifest.plugin_type is PluginType.CATALOG
        assert plugin.manifest.priority == 1000
        assert plugin.manifest.network is True

    def test_manifest_declares_the_catch_all_pattern(self) -> None:
        """Assert manifest declares the catch-all URL pattern."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        assert plugin.manifest.extract_url_patterns == (r"^https?://",)

    def test_manifest_declares_the_extract_urls_setting(self) -> None:
        """Assert the extract_urls url_list setting is declared."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        schema = plugin.manifest.settings_schema
        extract_urls_field = None
        for field in schema.fields:
            if field.key == "extract_urls":
                extract_urls_field = field
                break
        assert extract_urls_field is not None
        assert extract_urls_field.type == "url_list"

    def test_manifest_declares_the_metadata_settings(self) -> None:
        """Assert metadata settings fields are declared."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        schema = plugin.manifest.settings_schema
        field_keys = {field.key for field in schema.fields}
        assert "max_new_metadata_per_scan" in field_keys
        assert "request_delay_ms" in field_keys

        # Check max_new_metadata_per_scan
        for field in schema.fields:
            if field.key == "max_new_metadata_per_scan":
                assert field.type == "int"
                assert field.default == "25"
                break

        # Check request_delay_ms
        for field in schema.fields:
            if field.key == "request_delay_ms":
                assert field.type == "int"
                assert field.default == "750"
                break


class TestClaimsUrl:
    """Test the claims_url method."""

    def test_claims_any_http_url(self) -> None:
        """Assert claims_url returns True for http(s) URLs."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        assert plugin.claims_url("http://x.test/a") is True
        assert plugin.claims_url("https://x.test/a") is True

    def test_does_not_claim_a_non_http_url(self) -> None:
        """Assert claims_url returns False for non-http URLs."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        assert plugin.claims_url("ftp://x.test/a") is False
        assert plugin.claims_url("literotica.com/s/a") is False

    def test_claims_url_uses_the_manifest_patterns(self) -> None:
        """Assert claims_url matches patterns from manifest.extract_url_patterns."""
        plugin = UrlStoryExtractorPlugin(pages=FakePages())
        # Test the match
        assert plugin.claims_url("https://x.test/a") is True
        assert plugin.claims_url("ftp://x.test/a") is False
        # Verify the patterns are compiled from the manifest
        assert [p.pattern for p in plugin._patterns] == list(plugin.manifest.extract_url_patterns)

    def test_module_has_no_second_pattern_constant(self) -> None:
        """Assert no _COMPILED_PATTERNS constant exists in the module."""
        from pathlib import Path

        text = Path(__file__).resolve().parents[1] / "url_story_extractor" / "plugin.py"
        text_content = text.read_text()
        assert "_COMPILED_PATTERNS" not in text_content


class TestExtractStories:
    """Test the extract_stories method."""

    def test_extract_builds_one_patch_per_url(self) -> None:
        """Assert one patch is created per returned URL."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://a.test/s1", "https://b.test/s2"]}
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/l", ctx)
        assert len(patches) == 2
        assert patches[0].url == "https://a.test/s1"
        assert patches[1].url == "https://b.test/s2"

    def test_extract_derives_a_readable_title(self) -> None:
        """Assert title is derived from the URL path."""
        pages = FakePages(
            urls_by_listing={"https://x.test/l": ["https://x.test/s/the-senators-daughter-ch-01"]}
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/l", ctx)
        assert len(patches) == 1
        assert patches[0].title == "The Senators Daughter Ch. 01"

    def test_extract_falls_back_to_the_url_as_title(self) -> None:
        """Assert title falls back to the full URL when no path."""
        pages = FakePages(urls_by_listing={"https://x.test/l": ["https://x.test"]})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/l", ctx)
        assert len(patches) == 1
        assert patches[0].title == "https://x.test"

    def test_extract_sets_no_story_id(self) -> None:
        """Assert story_id is not set (core derives it)."""
        pages = FakePages(urls_by_listing={"https://x.test/l": ["https://x.test/s/story"]})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/l", ctx)
        assert len(patches) == 1
        assert patches[0].story_id is None

    def test_extract_sets_the_site(self) -> None:
        """Assert site is derived from the story URL."""
        pages = FakePages(urls_by_listing={"https://x.test/l": ["https://www.literotica.com/s/a"]})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/l", ctx)
        assert len(patches) == 1
        assert patches[0].site == "literotica.com"

    def test_extract_sets_the_author_from_the_listing_url(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert author is derived from the listing URL."""
        pages = FakePages(
            urls_by_listing={
                "https://www.literotica.com/authors/jane-doe/works/stories": [
                    "https://www.literotica.com/s/a"
                ]
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories(
            "https://www.literotica.com/authors/jane-doe/works/stories", ctx
        )
        assert len(patches) == 1
        assert patches[0].author == "Jane Doe"

    def test_extract_leaves_author_none_for_an_unrecognised_listing(self) -> None:
        """Assert author is None when listing URL is not recognized."""
        pages = FakePages(urls_by_listing={"https://x.test/tags/romance": ["https://x.test/s/a"]})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/tags/romance", ctx)
        assert len(patches) == 1
        assert patches[0].author is None

    def test_extract_sets_the_author_url_from_the_listing_url(self) -> None:
        """Assert author_url is derived from the listing URL."""
        pages = FakePages(
            urls_by_listing={
                "https://www.literotica.com/authors/Morgan_Ellis/works/stories": [
                    "https://www.literotica.com/s/joan-of-snark-ch-02"
                ]
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories(
            "https://www.literotica.com/authors/Morgan_Ellis/works/stories", ctx
        )
        assert len(patches) == 1
        assert patches[0].author_url == "https://www.literotica.com/authors/Morgan_Ellis"

    def test_extract_sets_author_and_author_url_together(self) -> None:
        """Assert author and author_url are both set from the listing URL."""
        pages = FakePages(
            urls_by_listing={
                "https://www.literotica.com/authors/Morgan_Ellis/works/stories": [
                    "https://www.literotica.com/s/joan-of-snark-ch-02"
                ]
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories(
            "https://www.literotica.com/authors/Morgan_Ellis/works/stories", ctx
        )
        assert len(patches) == 1
        assert patches[0].author == "Morgan Ellis"
        assert patches[0].author_url == "https://www.literotica.com/authors/Morgan_Ellis"

    def test_extract_leaves_author_url_none_for_an_unrecognised_listing(self) -> None:
        """Assert author_url is None when listing URL is not recognized."""
        pages = FakePages(urls_by_listing={"https://x.test/tags/romance": ["https://x.test/s/a"]})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/tags/romance", ctx)
        assert len(patches) == 1
        assert patches[0].author_url is None

    def test_extract_author_url_is_independent_of_the_story_url(self) -> None:
        """Assert author_url from listing URL applies to all stories."""
        pages = FakePages(
            urls_by_listing={
                "https://x.test/author/jane-doe": [
                    "https://other.test/s/a-tale",
                    "https://other.test/s/b-tale",
                ]
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        patches = plugin.extract_stories("https://x.test/author/jane-doe", ctx)
        assert len(patches) == 2
        assert patches[0].author_url == "https://x.test/author/jane-doe"
        assert patches[1].author_url == "https://x.test/author/jane-doe"

    def test_extract_drops_an_unusable_url(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert unusable URLs are dropped and logged."""
        pages = FakePages(urls_by_listing={"https://x.test/l": ["not a url", "https://x.test/s/a"]})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            logger=caplog.records[0].getLogger() if caplog.records else logging.getLogger("test")
        )
        with caplog.at_level(logging.DEBUG):
            patches = plugin.extract_stories("https://x.test/l", ctx)
        assert len(patches) == 1
        assert "Skipped an unusable story URL" in caplog.text

    def test_extract_of_an_empty_page_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert warning is logged when no stories found."""
        pages = FakePages(urls_by_listing={"https://x.test/l": []})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        with caplog.at_level(logging.WARNING):
            patches = plugin.extract_stories("https://x.test/l", ctx)
        assert len(patches) == 0
        assert "No stories found at" in caplog.text

    def test_extract_logs_entry_and_count(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert INFO logs at entry and exit."""
        pages = FakePages(
            urls_by_listing={"https://x.test/a": ["https://s1.test/s", "https://s2.test/s"]}
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx()
        with caplog.at_level(logging.INFO):
            plugin.extract_stories("https://x.test/a", ctx)
        assert "Extracting stories from https://x.test/a" in caplog.text
        assert "Extracted 2 story(ies) from https://x.test/a" in caplog.text


class TestScan:
    """Test the scan method."""

    def test_scan_covers_every_watched_url(self) -> None:
        """Assert scan calls extract_stories for each watched URL."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://a.test/s1"],
                "https://b.test/l": ["https://b.test/s1"],
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"extract_urls": ["https://a.test/l", "https://b.test/l"]})
        patches = plugin.scan(ctx)
        assert len(patches) == 2
        assert set(pages.calls) == {"https://a.test/l", "https://b.test/l"}

    def test_scan_deduplicates_across_watched_urls(self) -> None:
        """Assert duplicate URLs across listings are de-duplicated."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://shared.test/s"],
                "https://b.test/l": ["https://shared.test/s"],
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"extract_urls": ["https://a.test/l", "https://b.test/l"]})
        patches = plugin.scan(ctx)
        assert len(patches) == 1
        assert patches[0].url == "https://shared.test/s"

    def test_scan_with_no_watched_urls_logs_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert INFO is logged (not WARNING) when no watched URLs configured."""
        pages = FakePages()
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"extract_urls": []})
        caplog.set_level(logging.INFO)
        patches = plugin.scan(ctx)
        assert len(patches) == 0
        assert "Scan skipped: no watched URL is configured" in caplog.text
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    def test_scan_with_a_missing_setting_logs_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert INFO is logged when extract_urls setting is missing entirely."""
        pages = FakePages()
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={})
        caplog.set_level(logging.INFO)
        patches = plugin.scan(ctx)
        assert len(patches) == 0
        assert "Scan skipped: no watched URL is configured" in caplog.text
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    def test_scan_with_a_missing_setting_is_empty(self) -> None:
        """Assert empty result when extract_urls setting is missing."""
        pages = FakePages()
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={})
        patches = plugin.scan(ctx)
        assert len(patches) == 0

    def test_scan_logs_start_and_finish(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert INFO logs at start and finish."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://a.test/s1"],
                "https://b.test/l": ["https://b.test/s2"],
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"extract_urls": ["https://a.test/l", "https://b.test/l"]})
        with caplog.at_level(logging.INFO):
            plugin.scan(ctx)
        assert "Scan started: 2 watched URL(s)" in caplog.text
        assert "Scan finished: 2 story(ies) from 2 URL(s)" in caplog.text
        assert "metadata fetch(es) unspent" in caplog.text

    def test_scan_continues_past_one_empty_listing(self) -> None:
        """Assert scan continues when one listing returns no URLs."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": [],
                "https://b.test/l": ["https://b.test/s1"],
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"extract_urls": ["https://a.test/l", "https://b.test/l"]})
        patches = plugin.scan(ctx)
        assert len(patches) == 1
        assert patches[0].url == "https://b.test/s1"

    def test_scan_lists_every_url_before_enriching(self) -> None:
        """Assert listing is unbudgeted and enrichment is a single pass."""
        pages = FakePages(
            urls_by_listing={
                "https://a.test/l": ["https://a.test/s1"],
                "https://b.test/l": ["https://b.test/s1"],
                "https://c.test/l": ["https://c.test/s1"],
            }
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={
                "extract_urls": ["https://a.test/l", "https://b.test/l", "https://c.test/l"],
                "max_new_metadata_per_scan": 1,
                "request_delay_ms": 0,
            }
        )
        plugin.scan(ctx)
        # All three listings should be listed
        assert len(pages.calls) == 3
        # Only one fetch because of cap
        assert len(pages.metadata_calls) == 1
