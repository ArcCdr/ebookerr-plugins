"""Tests for the URL Story Extractor gateway passing a site's browser check (FR-CHS-10)."""

from __future__ import annotations

import email.message
import logging
import urllib.error
from typing import Any
from unittest import mock

import pytest
from ebookerr_sdk.challenge import ChallengeRouter
from ebookerr_sdk.challenge.solver import Clearance, ClearanceCookie, Solution
from ebookerr_sdk.testing import FakeContext
from fanficfare import exceptions
from url_story_extractor.pages import FanFicFarePagesGateway, ListingError

_URL = "https://www.example.org/s/1"


def _check(url: str) -> urllib.error.HTTPError:
    """A Cloudflare browser check as ``urlopen`` raises it: 403 with ``cf-mitigated: challenge``."""
    msg = email.message.Message()
    msg["cf-mitigated"] = "challenge"
    return urllib.error.HTTPError(url, 403, "Forbidden", msg, None)


def _ok_response() -> mock.MagicMock:
    """A stand-in for what ``urlopen`` returns: a context manager."""
    return mock.MagicMock(
        __enter__=mock.MagicMock(
            return_value=mock.MagicMock(read=mock.MagicMock(return_value=b""))
        ),
        __exit__=mock.MagicMock(return_value=None),
    )


class FakeSolver:
    """A challenge solver double: ``.url`` is set and ``solve`` returns a fixed clearance."""

    def __init__(self) -> None:
        """Patch ``SolverClient`` to return the fixed clearance."""
        self.url = "https://fake-solver.example"
        solution = Solution(
            url=_URL,
            status=200,
            body="",
            clearance=Clearance(
                user_agent="Mozilla/5.0 (FakeSolver) Chrome/152.0",
                cookies=(ClearanceCookie("clearance", "fake-value", domain="example.org"),),
            ),
            expires_at=None,
        )
        self._patch = mock.patch(
            "ebookerr_sdk.challenge.router.SolverClient",
            return_value=mock.Mock(solve=mock.Mock(return_value=solution)),
        )

    def __enter__(self) -> FakeSolver:
        """Start the ``SolverClient`` patch."""
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the ``SolverClient`` patch."""
        self._patch.stop()


def _forbidden() -> exceptions.HTTPErrorFFF:
    """FanFicFare's own 403."""
    return exceptions.HTTPErrorFFF(_URL, 403, "Forbidden")


def _router(ctx: FakeContext) -> ChallengeRouter:
    """The router a plugin builds from its context."""
    return ChallengeRouter(ctx)


@pytest.mark.pins("FR-CHS-10")
def test_a_challenged_listing_without_a_solver_reads_the_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser check and no solver: the sentence, and the listing fails as a whole."""
    lister = mock.Mock(side_effect=_forbidden())
    gateway = FanFicFarePagesGateway({}, lister=lister, router=_router(FakeContext()))

    with (
        mock.patch("urllib.request.urlopen", side_effect=_check(_URL)),
        pytest.raises(ListingError) as info,
    ):
        gateway.list_story_urls(_URL)

    assert str(info.value) == (
        "could not list stories at https://www.example.org/s/1: "
        "example.org asks for a browser check — set a challenge solver in Settings → General"
    )


@pytest.mark.pins("FR-CHS-10")
def test_a_challenged_listing_lists_once_more_with_the_clearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe routes the host, FanFicFare lists once more and gets the clearance's User-Agent."""
    configs: list[Any] = []
    calls = {"n": 0}

    def lister(url: str, configuration: Any, normalize: bool) -> dict[str, Any]:
        """Fail the first time with a 403, then list."""
        configs.append(configuration)
        calls["n"] += 1
        if calls["n"] == 1:
            raise _forbidden()
        return {"urllist": ["https://www.example.org/s/2"]}

    with FakeSolver() as fake:
        gateway = FanFicFarePagesGateway(
            {},
            lister=lister,
            router=_router(FakeContext(app_settings={"challenge_solver_url": fake.url})),
        )
        with mock.patch("urllib.request.urlopen", side_effect=[_check(_URL), _ok_response()]):
            urls = gateway.list_story_urls(_URL)

    assert urls == ["https://www.example.org/s/2"]
    assert calls["n"] == 2
    assert configs[1].getConfig("user_agent") == "Mozilla/5.0 (FakeSolver) Chrome/152.0"


@pytest.mark.pins("FR-CHS-10")
def test_a_challenged_metadata_poll_logs_the_sentence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A metadata poll and no solver: None and the sentence on a WARNING."""
    fetcher = mock.Mock(side_effect=_forbidden())
    gateway = FanFicFarePagesGateway({}, metadata_fetcher=fetcher, router=_router(FakeContext()))
    caplog.set_level(logging.WARNING)

    with mock.patch("urllib.request.urlopen", side_effect=_check(_URL)):
        result = gateway.fetch_story_metadata(_URL)

    assert result is None
    assert (
        "url_story_extractor.pages",
        logging.WARNING,
        "Could not fetch metadata for https://www.example.org/s/1: "
        "example.org asks for a browser check — set a challenge solver in Settings → General",
    ) in caplog.record_tuples


@pytest.mark.pins("FR-CHS-10")
def test_the_default_metadata_fetcher_lets_a_403_reach_the_challenge_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default poll does not swallow FanFicFare's 403: no solver, the sentence on a WARNING."""
    gateway = FanFicFarePagesGateway({}, router=_router(FakeContext()))
    caplog.set_level(logging.WARNING)

    with (
        mock.patch.object(FanFicFarePagesGateway, "_configuration", return_value=mock.Mock()),
        mock.patch("url_story_extractor.pages.adapters.getAdapter", side_effect=_forbidden()),
        mock.patch("urllib.request.urlopen", side_effect=_check(_URL)),
    ):
        result = gateway.fetch_story_metadata(_URL)

    assert result is None
    assert (
        "url_story_extractor.pages",
        logging.WARNING,
        "Could not fetch metadata for https://www.example.org/s/1: "
        "example.org asks for a browser check — set a challenge solver in Settings → General",
    ) in caplog.record_tuples


@pytest.mark.pins("FR-CHS-10")
def test_the_default_metadata_fetcher_polls_once_more_after_the_probe_routes_the_host() -> None:
    """A 403 from the default poll is probed; the routed host is polled once more and answers."""
    adapter = mock.Mock()
    adapter.getStoryMetadataOnly.return_value.getAllMetadata.return_value = {"title": "T"}
    get_adapter = mock.Mock(side_effect=[_forbidden(), adapter])

    with FakeSolver() as fake:
        gateway = FanFicFarePagesGateway(
            {}, router=_router(FakeContext(app_settings={"challenge_solver_url": fake.url}))
        )
        with (
            mock.patch.object(FanFicFarePagesGateway, "_configuration", return_value=mock.Mock()),
            mock.patch("url_story_extractor.pages.adapters.getAdapter", get_adapter),
            mock.patch("urllib.request.urlopen", side_effect=[_check(_URL), _ok_response()]),
        ):
            result = gateway.fetch_story_metadata(_URL)

    assert result == {"title": "T"}
    assert get_adapter.call_count == 2


@pytest.mark.pins("FR-CHS-10")
def test_a_clearance_fanficfare_is_refused_sends_the_third_listing_through_the_solver() -> None:
    """Probe passes, the rerun still gets 403: the host goes every page and lists a third time."""
    configs: list[Any] = []
    outcomes: list[Any] = [_forbidden(), _forbidden(), {"urllist": ["https://www.example.org/s/2"]}]

    def lister(url: str, configuration: Any, normalize: bool) -> dict[str, Any]:
        """Fail twice with a 403, then list."""
        configs.append(configuration)
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with FakeSolver() as fake:
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        gateway = FanFicFarePagesGateway({}, lister=lister, router=_router(ctx))
        with (
            mock.patch("urllib.request.urlopen", side_effect=[_check(_URL), _ok_response()]),
            mock.patch("ebookerr_sdk.challenge.router.SolverClient.health", return_value=True),
        ):
            urls = gateway.list_story_urls(_URL)

    assert urls == ["https://www.example.org/s/2"]
    assert len(configs) == 3
    assert ctx.state_get("challenge.every_page:www.example.org") is True
    assert configs[2].getConfig("use_flaresolverr_proxy") == "directimages"
