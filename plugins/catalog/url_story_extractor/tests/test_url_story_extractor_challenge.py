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
