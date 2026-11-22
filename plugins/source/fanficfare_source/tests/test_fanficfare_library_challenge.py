"""Tests for the FanFicFare library gateway passing a site's browser check (FR-CHS-10)."""

from __future__ import annotations

import email.message
import logging
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from ebookerr_sdk.challenge import ChallengeRouter
from ebookerr_sdk.challenge.solver import Clearance, ClearanceCookie, Solution
from ebookerr_sdk.testing import FakeCircuit, FakeContext
from fanficfare import exceptions
from fanficfare_source import library
from fanficfare_source.library import FanFicFareLibraryGateway

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


class _Adapter:
    """A fake FanFicFare adapter with one metadata mapping and no chapter."""

    def getStoryMetadataOnly(self) -> _Adapter:  # noqa: N802 — FanFicFare's name
        """Return itself."""
        return self

    def getAllMetadata(self) -> dict[str, str]:  # noqa: N802 — FanFicFare's name
        """Return the metadata."""
        return {"title": "T"}

    def get_chapters(self) -> list[dict[str, str]]:
        """Return no chapter."""
        return []


@pytest.fixture(autouse=True)
def _example_site_has_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the example host the ``unknown`` section, as FanFicFare supports no such site."""
    monkeypatch.setattr(library.adapters, "getConfigSectionsFor", lambda url: ["unknown"])


def _forbidden() -> exceptions.HTTPErrorFFF:
    """FanFicFare's own 403."""
    return exceptions.HTTPErrorFFF(_URL, 403, "Forbidden")


def _router(ctx: FakeContext) -> ChallengeRouter:
    """The router a plugin builds from its context."""
    return ChallengeRouter(ctx)


@pytest.mark.pins("FR-CHS-10")
def test_a_challenged_fetch_without_a_solver_reads_the_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser check and no solver: the sentence, and the breaker is not told of a failure."""
    monkeypatch.setattr(library.adapters, "getAdapter", mock.Mock(side_effect=_forbidden()))
    circuit = FakeCircuit()
    gateway = FanFicFareLibraryGateway({}, circuit=circuit, router=_router(FakeContext()))

    with mock.patch("urllib.request.urlopen", side_effect=_check(_URL)):
        fetch = gateway.fetch_metadata(_URL)

    assert (
        fetch.error
        == "example.org asks for a browser check — set a challenge solver in Settings → General"
    )
    assert circuit.failed == []


@pytest.mark.pins("FR-CHS-10")
def test_a_challenged_fetch_runs_fanficfare_once_more_with_the_clearance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The probe routes the host, FanFicFare runs once more and gets the clearance's User-Agent."""
    configs: list[Any] = []
    calls = {"n": 0}

    def get_adapter(config: Any, url: str) -> _Adapter:
        """Fail the first time with a 403, then answer."""
        configs.append(config)
        calls["n"] += 1
        if calls["n"] == 1:
            raise _forbidden()
        return _Adapter()

    monkeypatch.setattr(library.adapters, "getAdapter", get_adapter)
    caplog.set_level(logging.INFO)
    with FakeSolver() as fake:
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        gateway = FanFicFareLibraryGateway({}, router=_router(ctx))
        with mock.patch("urllib.request.urlopen", side_effect=[_check(_URL), _ok_response()]):
            fetch = gateway.fetch_metadata(_URL)

    assert fetch.meta is not None
    assert fetch.meta["title"] == "T"
    assert len(configs) == 2
    assert configs[1].getConfig("user_agent") == "Mozilla/5.0 (FakeSolver) Chrome/152.0"
    assert (
        "fanficfare_source.library",
        logging.INFO,
        "Running FanFicFare once more for https://www.example.org/s/1: "
        "its site's browser check is now routed",
    ) in caplog.record_tuples


@pytest.mark.pins("FR-CHS-10")
def test_a_host_checked_on_every_page_with_a_silent_solver_reads_did_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host marked every page and a solver that is down: the did-not-answer sentence."""
    get_adapter = mock.Mock()
    monkeypatch.setattr(library.adapters, "getAdapter", get_adapter)
    ctx = FakeContext(app_settings={"challenge_solver_url": "http://127.0.0.1:9"})
    ctx.state_set("challenge.every_page:www.example.org", True)

    fetch = FanFicFareLibraryGateway({}, router=_router(ctx)).fetch_metadata(_URL)

    assert fetch.error == (
        "example.org asks for a browser check, and the challenge solver at "
        "Settings → General did not answer"
    )
    get_adapter.assert_not_called()


@pytest.mark.pins("FR-CHS-10")
def test_a_download_that_fails_again_after_the_retry_keeps_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second 403 after the retry is the plain HTTP 403 failure, with exactly two runs."""
    get_adapter = mock.Mock(side_effect=[_forbidden(), _forbidden()])
    monkeypatch.setattr(library.adapters, "getAdapter", get_adapter)
    with FakeSolver() as fake:
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        gateway = FanFicFareLibraryGateway({}, router=_router(ctx))
        with mock.patch("urllib.request.urlopen", side_effect=[_check(_URL), _ok_response()]):
            result = gateway.download(_URL, work_dir=tmp_path, staged_filename=None)

    assert result.outcome == "failed"
    assert result.error == "the site answered HTTP 403"
    assert get_adapter.call_count == 2
