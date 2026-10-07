"""FanFicFarePagesGateway caches its FanFicFare Configuration per section set."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from url_story_extractor.pages import FanFicFarePagesGateway


class _CountingConfig:
    """Stand-in for FanFicFare's Configuration that counts read() calls."""

    reads = 0

    def __init__(self, sections, fmt, lightweight=False):  # noqa: D107, ANN001, ANN204
        """Initialize the counting configuration.

        Args:
            sections: The configuration sections.
            fmt: The format (e.g., 'EPUB').
            lightweight: Whether to use lightweight mode.
        """
        self.sections = list(sections)

    def read(self, paths):  # noqa: ANN001, ANN201, D102
        """Increment read counter and do nothing else."""
        type(self).reads += 1

    def read_string(self, text):  # noqa: ANN001, ANN201, D102
        """Accept the base options without counting a parse."""

    def has_section(self, section):  # noqa: ANN001, ANN201, D102
        """Report every section present, so no section is added."""
        return True

    def set(self, section, option, value):  # noqa: ANN001, ANN201, D102
        """Ignore a per-call override."""


def test_two_calls_for_the_same_sections_parse_the_ini_once(monkeypatch: Any) -> None:
    """Two calls with the same sections parse the ini files once."""
    _CountingConfig.reads = 0

    def mock_get_config_sections_for(url: str) -> list[str]:  # noqa: ANN001, ARG001
        """Always return the same sections."""
        return ["a"]

    monkeypatch.setattr(
        "url_story_extractor.fff_support.adapters.getConfigSectionsFor",
        mock_get_config_sections_for,
    )
    monkeypatch.setattr("url_story_extractor.fff_support.Configuration", _CountingConfig)

    gateway = FanFicFarePagesGateway(
        {},
        lister=lambda u, c, n: {"urllist": []},  # noqa: ANN001
        metadata_fetcher=lambda u, c: None,  # noqa: ANN001
    )

    gateway.list_story_urls("https://x.test/a")
    gateway.fetch_story_metadata("https://x.test/b")

    assert _CountingConfig.reads == 1


def test_different_sections_get_their_own_configuration(monkeypatch: Any) -> None:
    """Different sections result in separate configuration parses."""
    _CountingConfig.reads = 0

    def mock_get_config_sections_for(url: str) -> list[str]:  # noqa: ANN001
        """Return 'a' for URLs containing 'a.test', 'b' otherwise."""
        if "a.test" in url:
            return ["a"]
        return ["b"]

    monkeypatch.setattr(
        "url_story_extractor.fff_support.adapters.getConfigSectionsFor",
        mock_get_config_sections_for,
    )
    monkeypatch.setattr("url_story_extractor.fff_support.Configuration", _CountingConfig)

    gateway = FanFicFarePagesGateway(
        {},
        lister=lambda u, c, n: {"urllist": []},  # noqa: ANN001
        metadata_fetcher=lambda u, c: None,  # noqa: ANN001
    )

    gateway.fetch_story_metadata("https://a.test/s/1")
    gateway.fetch_story_metadata("https://b.test/s/2")

    assert _CountingConfig.reads == 2


def test_a_failed_parse_is_not_cached(monkeypatch: Any) -> None:
    """A failed parse is not cached and can be retried."""

    call_count = [0]

    class _FailOnceConfig:
        """Configuration that fails on first read, succeeds on retry."""

        def __init__(self, sections, fmt, lightweight=False):  # noqa: D107, ANN001, ANN204
            """Initialize."""
            self.sections = list(sections)

        def read(self, paths):  # noqa: ANN001, D102
            """Raise on first call, succeed on retry."""
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("boom")

        def read_string(self, text):  # noqa: ANN001, ANN201, D102
            """Accept the base options."""

        def has_section(self, section):  # noqa: ANN001, ANN201, D102
            """Report every section present, so no section is added."""
            return True

        def set(self, section, option, value):  # noqa: ANN001, ANN201, D102
            """Ignore a per-call override."""

    def mock_get_config_sections_for(url: str) -> list[str]:  # noqa: ANN001, ARG001
        """Always return the same sections."""
        return ["a"]

    monkeypatch.setattr(
        "url_story_extractor.fff_support.adapters.getConfigSectionsFor",
        mock_get_config_sections_for,
    )
    monkeypatch.setattr("url_story_extractor.fff_support.Configuration", _FailOnceConfig)

    gateway = FanFicFarePagesGateway({}, lister=lambda u, c, n: {"urllist": []})  # noqa: ANN001

    # First call should raise
    with pytest.raises(RuntimeError) as exc_info:
        gateway._configuration("https://x.test/a")
    assert str(exc_info.value) == "Could not parse FanFicFare configuration: ValueError"
    assert "boom" not in str(exc_info.value)

    # Second call should succeed (not cached)
    result = gateway._configuration("https://x.test/a")
    assert result is not None


def test_configuration_reuse_logs_at_debug(monkeypatch: Any, caplog: Any) -> None:
    """Configuration reuse logs at DEBUG level."""
    caplog.set_level(logging.DEBUG, logger="url_story_extractor.pages")
    _CountingConfig.reads = 0

    def mock_get_config_sections_for(url: str) -> list[str]:  # noqa: ANN001, ARG001
        """Always return the same sections."""
        return ["a"]

    monkeypatch.setattr(
        "url_story_extractor.fff_support.adapters.getConfigSectionsFor",
        mock_get_config_sections_for,
    )
    monkeypatch.setattr("url_story_extractor.fff_support.Configuration", _CountingConfig)

    gateway = FanFicFarePagesGateway({})

    gateway._configuration("https://x.test/a")
    gateway._configuration("https://x.test/b")

    assert "Built a FanFicFare configuration for sections=a" in caplog.text
    assert "Reusing the FanFicFare configuration for sections=a" in caplog.text


def test_listing_failure_raises(monkeypatch: Any, caplog: Any) -> None:
    """Listing failure raises ListingError and does not reference elapsed."""
    from url_story_extractor.pages import ListingError

    caplog.set_level(logging.WARNING)

    def mock_get_config_sections_for(url: str) -> list[str]:  # noqa: ANN001, ARG001
        """Return a section."""
        return ["a"]

    monkeypatch.setattr(
        "url_story_extractor.fff_support.adapters.getConfigSectionsFor",
        mock_get_config_sections_for,
    )

    gateway = FanFicFarePagesGateway(
        {},
        lister=lambda u, c, n: (_ for _ in ()).throw(RuntimeError("nope")),  # noqa: ANN001
    )

    with pytest.raises(ListingError):
        gateway.list_story_urls("https://x.test/a")


def test_no_dead_elapsed_assignment_in_the_failure_branch() -> None:
    """The dead elapsed assignment is removed from the except branch."""
    source = Path(__file__).resolve().parents[1] / "url_story_extractor" / "pages.py"
    source_text = source.read_text()
    count = source_text.count("elapsed = time.monotonic() - start")
    expected = 2
    assert count == expected, (
        f"Expected {expected} occurrences of 'elapsed = time.monotonic() - start', found {count}"
    )


def test_listing_retry_budget_is_capped() -> None:
    """Listing retry budget is capped to one retry through max_request_retries (EXP-073)."""
    gateway = FanFicFarePagesGateway({})

    config = gateway._configuration("http://test1.com?sid=1")

    assert config.getConfig("max_request_retries") == "1"
    assert config.get_fetcher().retries.total == 1
