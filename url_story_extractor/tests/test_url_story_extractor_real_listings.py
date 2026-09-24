"""Tests for URL extraction from real listing fixtures.

These tests pin real-world listings (Literotica, storiesonline, Royal Road)
captured live through FanFicFarePagesGateway, exercising the URL tier and
metadata enrichment of UrlStoryExtractorPlugin in isolation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ebookerr_sdk.domain.chapter_number import extract_chapter_info
from ebookerr_sdk.spi import InvocationMode, PluginEventType
from url_story_extractor.plugin import UrlStoryExtractorPlugin

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> list[str]:
    """Return one recorded listing's URLs."""
    return json.loads((_FIXTURES / name).read_text())


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


class TestFixtureIntegrity:
    """Test that fixture files have correct structure and content."""

    def test_literotica_fixture_has_77_urls(self) -> None:
        """Assert literotica fixture contains exactly 77 URLs."""
        assert len(load("literotica_morgan_ellis_urls.json")) == 77

    def test_storiesonline_fixture_has_96_urls(self) -> None:
        """Assert storiesonline fixture contains exactly 96 URLs."""
        assert len(load("storiesonline_caleb_urls.json")) == 96

    def test_royalroad_fixture_has_one_url(self) -> None:
        """Assert royalroad fixture contains exactly one URL."""
        assert load("royalroad_journey_urls.json") == ["https://www.royalroad.com/fiction/26675"]

    def test_no_fixture_url_carries_a_fragment(self) -> None:
        """Assert no URL in any fixture contains a fragment (#)."""
        for name in [
            "literotica_morgan_ellis_urls.json",
            "storiesonline_caleb_urls.json",
            "royalroad_journey_urls.json",
        ]:
            urls = load(name)
            for url in urls:
                assert "#" not in url, f"URL {url} in {name} contains a fragment"


class TestLiteroticaListing:
    """Test extraction from Literotica author page fixture."""

    _LISTING = "https://www.literotica.com/authors/Morgan_Ellis/works/stories"
    _FIXTURE_NAME = "literotica_morgan_ellis_urls.json"

    def test_literotica_yields_a_row_per_listed_url(self) -> None:
        """Assert one patch per URL in the listing."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == len(urls)

    def test_literotica_captures_every_angela_chapter(self) -> None:
        """Assert Angela's Stepfather chapters are all present."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        titles = {p.title for p in patches}

        expected = {
            "Angelas Stepfather Ch. 05",
            "Angelas Stepfather Ch. 04",
            "Angelas Stepfather Ch. 03",
            "Angelas Stepfather Ch. 02",
            "Angelas Stepfather Ch. 01",
        }
        assert expected.issubset(titles)

    def test_literotica_captures_every_joan_chapter(self) -> None:
        """Assert Joan of Snark chapters 1-17 are all present."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        titles = {p.title for p in patches}

        expected = {f"Joan Of Snark Ch. {n:02d}" for n in range(1, 18)}
        assert expected.issubset(titles)

    def test_literotica_captures_every_olivia_chapter(self) -> None:
        """Assert Olivia in Vulmonia chapters 1-47 are all present."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        titles = {p.title for p in patches}

        expected = {f"Olivia In Vulmonia Ch. {n:02d}" for n in range(1, 48)}
        assert expected.issubset(titles)

    def test_literotica_chapter_titles_parse_back_to_a_number(self) -> None:
        """Assert chapter titles extract to book name and chapter number."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        # Find the Joan ch-02 patch
        joan_ch2 = next(p for p in patches if p.url.endswith("joan-of-snark-ch-02"))
        info = extract_chapter_info(joan_ch2.title)

        assert info.book_name == "Joan Of Snark"
        assert info.chapter_number == "02"

    def test_literotica_sets_the_author_on_every_row(self) -> None:
        """Assert every patch has the listing author."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        for patch in patches:
            assert patch.author == "Morgan Ellis"

    def test_literotica_links_the_author_on_every_row(self) -> None:
        """Assert every patch has the listing author URL."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        for patch in patches:
            assert patch.author_url == "https://www.literotica.com/authors/Morgan_Ellis"

    def test_literotica_sets_the_site_on_every_row(self) -> None:
        """Assert every patch has site=literotica.com."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        for patch in patches:
            assert patch.site == "literotica.com"

    def test_literotica_rows_are_unique_by_url(self) -> None:
        """Assert all patches have distinct URLs."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        patch_urls = [p.url for p in patches]

        assert len(set(patch_urls)) == len(patches)

    def test_literotica_series_rows_keep_their_id_title(self) -> None:
        """Assert series URLs keep their ID as title until enriched."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        titles = {p.title for p in patches}

        # Series IDs should be in titles
        assert "81737137" in titles
        assert "133778587" in titles
        assert "493786767" in titles


class TestLiteroticaWithMetadata:
    """Test Literotica enrichment with metadata."""

    _LISTING = "https://www.literotica.com/authors/Morgan_Ellis/works/stories"
    _FIXTURE_NAME = "literotica_morgan_ellis_urls.json"

    def test_a_literotica_series_chapter_keeps_its_chapter_number(self) -> None:
        """Assert chapter title is preserved when parent is a series."""
        urls = load(self._FIXTURE_NAME)
        metadata = {
            "https://www.literotica.com/s/joan-of-snark-ch-02": {
                "storyUrl": "https://www.literotica.com/series/se/133778587",
                "title": "Joan of Snark",
                "numChapters": "17",
                "datePublished": "2019-10-15",
                "author": "Morgan_Ellis",
            }
        }
        pages = FakePages(urls_by_listing={self._LISTING: urls}, metadata_by_url=metadata)
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 100, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        patch = next(p for p in patches if p.url.endswith("joan-of-snark-ch-02"))

        assert patch.title == "Joan Of Snark Ch. 02"
        assert patch.series == "Joan of Snark"
        assert patch.series_url == "https://www.literotica.com/series/se/133778587"
        assert patch.num_chapters is None
        assert patch.date_published is None

    def test_a_literotica_series_row_takes_its_real_title(self) -> None:
        """Assert series row takes metadata title and chapter count."""
        urls = load(self._FIXTURE_NAME)
        metadata = {
            "https://www.literotica.com/series/se/81737137": {
                "storyUrl": "https://www.literotica.com/series/se/81737137",
                "title": "Olivia in Vulmonia",
                "numChapters": "47",
            }
        }
        pages = FakePages(urls_by_listing={self._LISTING: urls}, metadata_by_url=metadata)
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 100, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        patch = next(p for p in patches if "81737137" in p.url)

        assert patch.title == "Olivia in Vulmonia"
        assert patch.num_chapters == 47

    def test_a_literotica_standalone_row_takes_its_metadata(self) -> None:
        """Assert standalone story takes full metadata."""
        urls = load(self._FIXTURE_NAME)
        metadata = {
            "https://www.literotica.com/s/angelas-stepfather-ch-03": {
                "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                "title": "Angela's Stepfather Ch. 03",
                "numChapters": "1",
                "status": "Completed",
                "datePublished": "2026-07-08",
            }
        }
        pages = FakePages(urls_by_listing={self._LISTING: urls}, metadata_by_url=metadata)
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 100, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        patch = next(p for p in patches if p.url.endswith("angelas-stepfather-ch-03"))

        assert patch.title == "Angela's Stepfather Ch. 03"
        assert patch.num_chapters == 1
        assert patch.status == "Completed"
        assert patch.date_published is not None

    def test_the_confirmed_author_reaches_the_unfetched_rows(self) -> None:
        """Assert unanimously fetched author is applied to all rows."""
        urls = load(self._FIXTURE_NAME)
        # Provide metadata for only 3 URLs, all with Morgan_Ellis as author
        metadata = {
            "https://www.literotica.com/s/joan-of-snark-ch-02": {
                "storyUrl": "https://www.literotica.com/series/se/133778587",
                "title": "Joan of Snark",
                "numChapters": "17",
                "author": "Morgan_Ellis",
            },
            "https://www.literotica.com/series/se/81737137": {
                "storyUrl": "https://www.literotica.com/series/se/81737137",
                "title": "Olivia in Vulmonia",
                "numChapters": "47",
                "author": "Morgan_Ellis",
            },
            "https://www.literotica.com/s/angelas-stepfather-ch-03": {
                "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                "title": "Angela's Stepfather Ch. 03",
                "numChapters": "1",
                "author": "Morgan_Ellis",
            },
        }
        pages = FakePages(urls_by_listing={self._LISTING: urls}, metadata_by_url=metadata)
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 3, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        # Every patch should have Morgan_Ellis as author, even unfetched ones
        for patch in patches:
            assert patch.author == "Morgan_Ellis"


class TestStoriesonlineListing:
    """Test extraction from storiesonline fixture."""

    _LISTING = "https://storiesonline.net/s/29762/caleb-by-pastmaster"
    _FIXTURE_NAME = "storiesonline_caleb_urls.json"

    def test_storiesonline_yields_a_row_per_listed_url(self) -> None:
        """Assert one patch per URL in the listing."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 96

    def test_storiesonline_chapter_rows_are_named(self) -> None:
        """Assert chapter rows have book + number titles."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        titles = {p.title for p in patches}

        expected = {f"Caleb By Pastmaster {n}" for n in range(1, 96)}
        assert expected.issubset(titles)

    def test_no_storiesonline_row_is_a_bare_number(self) -> None:
        """Assert no patch title is only digits."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        for patch in patches:
            assert not patch.title.isdigit()

    def test_storiesonline_chapter_titles_parse_back_to_a_number(self) -> None:
        """Assert chapter titles extract to book name and chapter number."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        # Find the chapter 7 patch
        ch7 = next(p for p in patches if p.url.endswith("/7"))
        info = extract_chapter_info(ch7.title)

        assert info.book_name == "Caleb By Pastmaster"
        assert info.chapter_number == "7"

    def test_storiesonline_book_row_is_named(self) -> None:
        """Assert book URL has a title, not just an ID."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)
        titles = {p.title for p in patches}

        assert "Caleb By Pastmaster" in titles

    def test_storiesonline_sets_no_author_from_a_story_listing(self) -> None:
        """Assert listing URL (not author page) sets no author."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        for patch in patches:
            assert patch.author is None
            assert patch.author_url is None

    def test_storiesonline_sets_the_site(self) -> None:
        """Assert every patch has site=storiesonline.net."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        for patch in patches:
            assert patch.site == "storiesonline.net"


class TestRoyalRoadListing:
    """Test extraction from Royal Road fixture."""

    _LISTING = "https://www.royalroad.com/fiction/26675/a-journey-of-black-and-red"
    _FIXTURE_NAME = "royalroad_journey_urls.json"

    def test_royalroad_yields_the_canonical_fiction_row(self) -> None:
        """Assert Royal Road listing collapses to canonical fiction URL."""
        urls = load(self._FIXTURE_NAME)
        pages = FakePages(urls_by_listing={self._LISTING: urls})
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 0, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        assert patches[0].url == "https://www.royalroad.com/fiction/26675"

    def test_royalroad_row_takes_its_metadata_title(self) -> None:
        """Assert Royal Road row takes full metadata."""
        urls = load(self._FIXTURE_NAME)
        metadata = {
            "https://www.royalroad.com/fiction/26675": {
                "storyUrl": "https://www.royalroad.com/fiction/26675",
                "title": "A Journey of Black and Red",
                "author": "Mecanimus",
                "authorUrl": "https://www.royalroad.com/user/profile/105290",
                "numChapters": "239",
                "numWords": "1,260,944",
                "genre": "Action, Adventure, Fantasy",
                "status": "Completed",
            }
        }
        pages = FakePages(urls_by_listing={self._LISTING: urls}, metadata_by_url=metadata)
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(settings={"max_new_metadata_per_scan": 1, "request_delay_ms": 0})

        patches = plugin.extract_stories(self._LISTING, ctx)

        assert len(patches) == 1
        patch = patches[0]
        assert patch.title == "A Journey of Black and Red"
        assert patch.num_chapters == 239
        assert patch.num_words == 1260944
        assert patch.author == "Mecanimus"
        assert patch.author_url == "https://www.royalroad.com/user/profile/105290"
        assert patch.tags == "Action, Adventure, Fantasy"
        assert patch.status == "Completed"
