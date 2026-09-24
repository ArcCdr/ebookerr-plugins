"""Tests for the FanFicFare page-listing gateway."""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from url_story_extractor.pages import FanFicFarePagesGateway


class TestListStoryUrls:
    """Tests for the list_story_urls method."""

    def test_list_returns_the_urllist(self) -> None:
        """Returns all URLs from the lister's urllist key."""
        mock_lister = MagicMock(
            return_value={"urllist": ["https://x.test/s/a", "https://x.test/s/b"]}
        )
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == ["https://x.test/s/a", "https://x.test/s/b"]

    def test_list_deduplicates(self) -> None:
        """Removes duplicate URLs."""
        mock_lister = MagicMock(
            return_value={"urllist": ["https://x.test/s/a", "https://x.test/s/a"]}
        )
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == ["https://x.test/s/a"]

    def test_list_preserves_order(self) -> None:
        """Returns URLs in the same order as the lister."""
        mock_lister = MagicMock(
            return_value={
                "urllist": ["https://x.test/s/c", "https://x.test/s/a", "https://x.test/s/b"]
            }
        )
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == ["https://x.test/s/c", "https://x.test/s/a", "https://x.test/s/b"]

    def test_list_ignores_extra_keys(self) -> None:
        """Ignores extra keys like name and desc."""
        mock_lister = MagicMock(
            return_value={"urllist": ["https://x.test/s/u"], "name": "A Series", "desc": "..."}
        )
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == ["https://x.test/s/u"]

    def test_list_of_a_missing_urllist_is_empty(self) -> None:
        """Returns empty list when urllist key is missing."""
        mock_lister = MagicMock(return_value={})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == []

    def test_list_of_a_non_list_urllist_is_empty(self) -> None:
        """Returns empty list when urllist is not a list."""
        mock_lister = MagicMock(return_value={"urllist": "nope"})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == []

    def test_list_drops_non_string_entries(self) -> None:
        """Filters out non-string entries from urllist."""
        mock_lister = MagicMock(return_value={"urllist": ["https://x.test/s/u", 5, None]})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == ["https://x.test/s/u"]

    def test_list_raises_on_a_raising_lister(self, caplog: Any) -> None:
        """Raises ListingError when lister raises."""
        from url_story_extractor.pages import ListingError

        mock_lister = MagicMock(side_effect=RuntimeError("boom"))
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        match_pattern = r"could not list stories at https://x\.test/authors/jane: boom"
        with pytest.raises(ListingError, match=match_pattern) as info:
            gateway.list_story_urls("https://x.test/authors/jane")

        assert isinstance(info.value.__cause__, RuntimeError)
        assert any(
            "Listing failed at https://x.test/authors/jane: RuntimeError: boom" in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        )

    def test_an_empty_page_still_returns_an_empty_list(self, caplog: Any) -> None:
        """An empty listing page returns [] without raising."""
        caplog.set_level(logging.INFO)
        mock_lister = MagicMock(return_value={"urllist": []})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        result = gateway.list_story_urls("https://x.test/authors/jane")

        assert result == []
        messages = [record.message for record in caplog.records if record.levelno == logging.INFO]
        assert any(
            re.search(r"Listed 0 story URL\(s\) at https://x\.test/authors/jane", msg)
            for msg in messages
        )

    def test_list_logs_the_start_and_the_count(self, caplog: Any) -> None:
        """Logs INFO with start message and count message."""
        caplog.set_level(logging.INFO)
        mock_lister = MagicMock(
            return_value={"urllist": ["https://x.test/s/a", "https://x.test/s/b"]}
        )
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        gateway.list_story_urls("https://x.test/authors/jane")

        messages = [record.message for record in caplog.records if record.levelno == logging.INFO]
        assert any("Listing stories at https://x.test/authors/jane" in msg for msg in messages)
        assert any(
            re.search(r"Listed 2 story URL\(s\) at https://x\.test/authors/jane in \d+\.\d+s", msg)
            for msg in messages
        )

    def test_list_passes_the_url_to_the_lister(self) -> None:
        """Passes the URL as the first argument to lister."""
        mock_lister = MagicMock(return_value={"urllist": []})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        gateway.list_story_urls("https://x.test/authors/jane")

        mock_lister.assert_called_once()
        assert mock_lister.call_args[0][0] == "https://x.test/authors/jane"

    def test_list_asks_for_normalised_urls(self) -> None:
        """Passes normalize=True as the third argument to lister."""
        mock_lister = MagicMock(return_value={"urllist": ["https://x.test/s/a"]})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        gateway.list_story_urls("https://x.test/authors/jane")

        mock_lister.assert_called_once()
        assert mock_lister.call_args.args[0] == "https://x.test/authors/jane"
        assert mock_lister.call_args.args[2] is True

    def test_list_passes_the_configuration_positionally(self) -> None:
        """Passes configuration as the second positional argument."""
        mock_lister = MagicMock(return_value={"urllist": ["https://x.test/s/a"]})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), lister=mock_lister)

        gateway.list_story_urls("https://x.test/authors/jane")

        mock_lister.assert_called_once()
        assert len(mock_lister.call_args.args) == 3


class TestFetchStoryMetadata:
    """Tests for the fetch_story_metadata method."""

    def test_metadata_returns_the_dict(self) -> None:
        """Returns the metadata dict from the fetcher."""
        mock_fetcher = MagicMock(return_value={"title": "A Tale"})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), metadata_fetcher=mock_fetcher)

        result = gateway.fetch_story_metadata("https://x.test/s/a")

        assert result == {"title": "A Tale"}

    def test_metadata_returns_none_on_none(self) -> None:
        """Returns None when fetcher returns None."""
        mock_fetcher = MagicMock(return_value=None)
        gateway = FanFicFarePagesGateway(Path("personal.ini"), metadata_fetcher=mock_fetcher)

        result = gateway.fetch_story_metadata("https://x.test/s/a")

        assert result is None

    def test_metadata_swallows_a_raising_fetcher(self, caplog: Any) -> None:
        """Returns None and logs WARNING when fetcher raises."""
        mock_fetcher = MagicMock(side_effect=RuntimeError("boom"))
        gateway = FanFicFarePagesGateway(Path("personal.ini"), metadata_fetcher=mock_fetcher)

        result = gateway.fetch_story_metadata("https://x.test/s/a")

        assert result is None
        assert any(
            "Could not fetch metadata for" in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        )

    def test_metadata_logs_at_debug(self, caplog: Any) -> None:
        """Logs DEBUG messages for fetch start and completion."""
        caplog.set_level(logging.DEBUG)
        mock_fetcher = MagicMock(return_value={"title": "A Tale"})
        gateway = FanFicFarePagesGateway(Path("personal.ini"), metadata_fetcher=mock_fetcher)

        gateway.fetch_story_metadata("https://x.test/s/a")

        messages = [record.message for record in caplog.records if record.levelno == logging.DEBUG]
        assert any("Fetching metadata for https://x.test/s/a" in msg for msg in messages)
        assert any(
            re.search(r"Fetched metadata for https://x\.test/s/a in \d+\.\d+s", msg)
            for msg in messages
        )

    def test_no_secret_reaches_the_log(self, caplog: Any, tmp_path: Path) -> None:
        """Does not log personal.ini contents or secrets."""
        from url_story_extractor.pages import ListingError

        caplog.set_level(logging.DEBUG)
        personal_ini = tmp_path / "personal.ini"
        personal_ini.write_text("password:hunter2\n")

        mock_lister = MagicMock(return_value={"urllist": ["https://x.test/s/a"]})
        mock_fetcher = MagicMock(return_value={"title": "A Tale"})
        gateway = FanFicFarePagesGateway(
            personal_ini, lister=mock_lister, metadata_fetcher=mock_fetcher
        )

        with contextlib.suppress(ListingError):
            gateway.list_story_urls("https://x.test/authors/jane")
        gateway.fetch_story_metadata("https://x.test/s/a")

        all_log_text = "\n".join(record.message for record in caplog.records)
        assert "hunter2" not in all_log_text
