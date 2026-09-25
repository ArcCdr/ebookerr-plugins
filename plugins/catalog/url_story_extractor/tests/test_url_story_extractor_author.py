"""Tests for unanimous author propagation across extracted listings."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from ebookerr_sdk.spi import InvocationMode, PluginEventType, StoryPatch
from url_story_extractor.plugin import UrlStoryExtractorPlugin, _propagate_author


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


class TestPropagateAuthor:
    """Direct tests of the _propagate_author function."""

    def test_one_confirmed_author_fills_the_unfetched_rows(self) -> None:
        """Assert one confirmed author fills un-fetched rows with author and author_url."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author="Jane Doe",
                author_url=None,
                metadata_fetched=False,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author="Jane_Doe",
                author_url="https://x.test/authors/Jane_Doe",
                metadata_fetched=True,
            ),
        ]

        patches, applied, author = _propagate_author(input_patches)

        assert applied == 1
        assert author == "Jane_Doe"
        assert patches[0].author == "Jane_Doe"
        assert patches[0].author_url == "https://x.test/authors/Jane_Doe"

    def test_the_fetched_row_is_never_modified(self) -> None:
        """Assert fetched rows with metadata_fetched=True are never modified."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author="Jane Doe",
                author_url=None,
                metadata_fetched=False,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author="Jane_Doe",
                author_url="https://x.test/authors/Jane_Doe",
                metadata_fetched=True,
            ),
        ]

        patches, _, _ = _propagate_author(input_patches)

        assert patches[1].author == "Jane_Doe"
        assert patches[1].author_url == "https://x.test/authors/Jane_Doe"
        assert patches[1].metadata_fetched is True

    def test_two_confirmed_authors_propagate_nothing(self) -> None:
        """Assert mixed authors prevent propagation."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author="A",
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author="B",
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/c",
                author="Guess",
                author_url=None,
                metadata_fetched=False,
            ),
        ]

        patches, applied, author = _propagate_author(input_patches)

        assert applied == 0
        assert author == ""
        assert patches[2].author == "Guess"

    def test_no_fetched_row_propagates_nothing(self) -> None:
        """Assert no fetched rows means no propagation."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author="Guess A",
                author_url=None,
                metadata_fetched=False,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author="Guess B",
                author_url=None,
                metadata_fetched=False,
            ),
            StoryPatch(
                url="https://x.test/s/c",
                author="Guess C",
                author_url=None,
                metadata_fetched=False,
            ),
        ]

        patches, applied, author = _propagate_author(input_patches)

        assert applied == 0
        assert author == ""

    def test_a_fetched_row_with_no_author_is_ignored(self) -> None:
        """Assert fetched rows with no author don't prevent propagation of others."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author=None,
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author="Jane",
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/c",
                author="Guess",
                author_url=None,
                metadata_fetched=False,
            ),
        ]

        patches, applied, author = _propagate_author(input_patches)

        assert applied == 1
        assert author == "Jane"

    def test_author_url_comes_from_the_first_fetched_row_that_has_one(self) -> None:
        """Assert author_url is taken from the first fetched row that has one."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author="Jane",
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author="Jane",
                author_url="https://x.test/a",
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/c",
                author="Guess",
                author_url=None,
                metadata_fetched=False,
            ),
        ]

        patches, _, _ = _propagate_author(input_patches)

        assert patches[2].author_url == "https://x.test/a"

    def test_author_url_is_none_when_no_fetched_row_has_one(self) -> None:
        """Assert author_url is None when no fetched row carries one."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author="Jane",
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author="Guess",
                author_url=None,
                metadata_fetched=False,
            ),
        ]

        patches, _, _ = _propagate_author(input_patches)

        assert patches[1].author == "Jane"
        assert patches[1].author_url is None

    def test_authors_are_compared_decoded_and_stripped(self) -> None:
        """Assert authors are HTML-unescaped and stripped for comparison."""
        input_patches = [
            StoryPatch(
                url="https://x.test/s/a",
                author="Jane &amp; Co",
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/b",
                author=" Jane & Co ",
                author_url=None,
                metadata_fetched=True,
            ),
            StoryPatch(
                url="https://x.test/s/c",
                author="Guess",
                author_url=None,
                metadata_fetched=False,
            ),
            StoryPatch(
                url="https://x.test/s/d",
                author="Guess",
                author_url=None,
                metadata_fetched=False,
            ),
        ]

        patches, applied, author = _propagate_author(input_patches)

        assert applied == 2
        assert author == "Jane & Co"

    def test_order_is_preserved(self) -> None:
        """Assert returned list preserves original order."""
        input_patches = [
            StoryPatch(url="https://x.test/s/a", metadata_fetched=False),
            StoryPatch(url="https://x.test/s/b", metadata_fetched=True, author="A"),
            StoryPatch(url="https://x.test/s/c", metadata_fetched=False),
            StoryPatch(url="https://x.test/s/d", metadata_fetched=False),
            StoryPatch(url="https://x.test/s/e", metadata_fetched=True, author="A"),
        ]

        patches, _, _ = _propagate_author(input_patches)

        urls = [p.url for p in patches]
        assert urls == [
            "https://x.test/s/a",
            "https://x.test/s/b",
            "https://x.test/s/c",
            "https://x.test/s/d",
            "https://x.test/s/e",
        ]

    def test_an_empty_list_is_a_no_op(self) -> None:
        """Assert an empty list returns empty with no changes."""
        patches, applied, author = _propagate_author([])

        assert patches == []
        assert applied == 0
        assert author == ""


class TestAuthorPropagationEndToEnd:
    """End-to-end tests through the UrlStoryExtractorPlugin."""

    def test_an_author_page_ends_with_one_author_everywhere(self) -> None:
        """Assert all rows end up with the same author when it's unanimous."""
        pages = FakePages(
            urls_by_listing={
                "https://www.literotica.com/authors/Morgan_Ellis/works/stories": [
                    "https://www.literotica.com/s/a",
                    "https://www.literotica.com/s/b",
                    "https://www.literotica.com/s/c",
                ]
            },
            metadata_by_url={
                "https://www.literotica.com/s/a": {
                    "title": "T",
                    "author": "Morgan_Ellis",
                    "authorUrl": "https://www.literotica.com/authors/Morgan_Ellis/works/stories",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={"max_new_metadata_per_scan": 1, "request_delay_ms": 0},
            ui_context={},
        )

        patches = plugin.extract_stories(
            "https://www.literotica.com/authors/Morgan_Ellis/works/stories", ctx
        )

        assert len(patches) == 3
        for patch in patches:
            assert patch.author == "Morgan_Ellis"
            assert (
                patch.author_url == "https://www.literotica.com/authors/Morgan_Ellis/works/stories"
            )

    def test_author_propagation_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Assert propagation is logged at INFO level."""
        pages = FakePages(
            urls_by_listing={
                "https://www.literotica.com/authors/Morgan_Ellis/works/stories": [
                    "https://www.literotica.com/s/a",
                    "https://www.literotica.com/s/b",
                    "https://www.literotica.com/s/c",
                ]
            },
            metadata_by_url={
                "https://www.literotica.com/s/a": {
                    "title": "T",
                    "author": "Morgan_Ellis",
                    "authorUrl": "https://www.literotica.com/authors/Morgan_Ellis/works/stories",
                }
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={"max_new_metadata_per_scan": 1, "request_delay_ms": 0},
            ui_context={},
        )

        with caplog.at_level(logging.INFO):
            _ = plugin.extract_stories(
                "https://www.literotica.com/authors/Morgan_Ellis/works/stories", ctx
            )

        assert any(
            "Applied the listing's one confirmed author to 2 un-fetched row(s)" in record.message
            for record in caplog.records
        )

    def test_a_favourites_style_listing_is_not_relabelled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert mixed authors prevent relabelling."""
        pages = FakePages(
            urls_by_listing={
                "https://www.literotica.com/authors/Morgan_Ellis/favorites": [
                    "https://www.literotica.com/s/a",
                    "https://www.literotica.com/s/b",
                    "https://www.literotica.com/s/c",
                ]
            },
            metadata_by_url={
                "https://www.literotica.com/s/a": {
                    "title": "T1",
                    "author": "A",
                },
                "https://www.literotica.com/s/b": {
                    "title": "T2",
                    "author": "B",
                },
            },
        )
        plugin = UrlStoryExtractorPlugin(pages=pages)
        ctx = FakeCtx(
            settings={"max_new_metadata_per_scan": 2, "request_delay_ms": 0},
            ui_context={},
        )

        with caplog.at_level(logging.DEBUG):
            patches = plugin.extract_stories(
                "https://www.literotica.com/authors/Morgan_Ellis/favorites", ctx
            )

        # The third patch should keep its listing-derived author
        assert patches[2].author == "Morgan Ellis"
        # The propagation skip should be logged
        assert any(
            "Author propagation skipped" in record.message
            for record in caplog.records
            if record.levelname == "DEBUG"
        )
