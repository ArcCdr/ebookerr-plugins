"""Tests for the Patreon catalog telling a browser check from an expired session cookie (TR-CHS-4).

``_get_json`` reads every request through the SDK's ``open_url``: a Cloudflare browser check is
solved by the user's challenge solver, or the scan fails with the solver sentence. A 403 that is
not such a check still reads "Patreon session cookie missing or expired".
"""

from __future__ import annotations

import email.message
import importlib
import urllib.error
from unittest import mock

import pytest
from ebookerr_sdk.challenge import ChallengeBlockedError, ChallengeRouter
from ebookerr_sdk.challenge.solver import Clearance, ClearanceCookie, Solution
from ebookerr_sdk.testing import FakeCircuit, FakeContext

_MODULE = importlib.import_module("patreon_stories.catalog")


def _check(url: str) -> urllib.error.HTTPError:
    """A Cloudflare browser check as ``urlopen`` raises it: 403 with ``cf-mitigated: challenge``.

    Args:
        url: The request URL the error names.

    Returns:
        The browser-check error.
    """
    msg = email.message.Message()
    msg["cf-mitigated"] = "challenge"
    return urllib.error.HTTPError(url, 403, "Forbidden", msg, None)


def _plain_403(url: str) -> urllib.error.HTTPError:
    """A plain 403 refusal as ``urlopen`` raises it: no ``cf-mitigated`` header.

    Args:
        url: The request URL the error names.

    Returns:
        The refusal error.
    """
    headers = email.message.Message()
    return urllib.error.HTTPError(url, 403, "Forbidden", headers, None)


def _fake_solution() -> Solution:
    """A solver answer whose clearance is the clearance the passing tests assert."""
    return Solution(
        url="https://www.patreon.com/",
        status=200,
        body="<pre>OK</pre>",
        clearance=Clearance(
            user_agent="Mozilla/5.0 (FakeSolver) Chrome/152.0",
            cookies=(ClearanceCookie("clearance", "fake-clearance-value", domain="patreon.com"),),
        ),
        expires_at=None,
    )


class FakeSolver:
    """A challenge solver double: its ``.url`` is set and ``solve`` returns a fixed clearance.

    While active it patches ``SolverClient`` so ``solve`` returns :func:`_fake_solution`,
    without any network call.
    """

    def __init__(self) -> None:
        """Patch ``SolverClient`` to return the fixed clearance."""
        self.url = "https://fake-solver.example"
        self._patch = mock.patch(
            "ebookerr_sdk.challenge.router.SolverClient",
            return_value=mock.Mock(solve=mock.Mock(return_value=_fake_solution())),
        )

    def __enter__(self) -> FakeSolver:
        """Start the ``SolverClient`` patch."""
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the ``SolverClient`` patch."""
        self._patch.stop()


class _FakeResponse:
    """Minimal stand-in for what ``urlopen`` returns: a context manager reading *body*."""

    def __init__(self, body: bytes) -> None:
        """Store the body this fake will serve."""
        self._body = body

    def read(self, amt: int | None = None) -> bytes:
        """Return the canned body."""
        return self._body

    def __enter__(self) -> _FakeResponse:
        """Support ``with`` blocks."""
        return self

    def __exit__(self, *args: object) -> None:
        """Support ``with`` blocks."""
        return None


@pytest.mark.pins("TR-CHS-4")
def test_a_challenged_api_without_a_solver_reads_the_sentence() -> None:
    """A memberships call that gets a browser check and no solver reads the solver sentence."""
    with (
        mock.patch("urllib.request.urlopen", side_effect=_check(_MODULE.MEMBERSHIPS_URL)),
        pytest.raises(ChallengeBlockedError) as caught,
    ):
        _MODULE._get_json(
            _MODULE.MEMBERSHIPS_URL,
            "s",
            circuit=FakeCircuit(),
            router=ChallengeRouter(FakeContext()),
        )

    assert str(caught.value) == (
        "patreon.com asks for a browser check — set a challenge solver in Settings → General"
    )


@pytest.mark.pins("TR-CHS-4")
def test_a_plain_refusal_still_reads_the_expired_cookie_sentence() -> None:
    """A 403 without a browser check, with a solver, still reads the expired-cookie sentence."""
    with (
        mock.patch("urllib.request.urlopen", side_effect=_plain_403(_MODULE.MEMBERSHIPS_URL)),
        pytest.raises(RuntimeError) as caught,
    ):
        _MODULE._get_json(
            _MODULE.MEMBERSHIPS_URL,
            "s",
            circuit=FakeCircuit(),
            router=ChallengeRouter(FakeContext()),
        )

    assert str(caught.value) == "Patreon session cookie missing or expired"


@pytest.mark.pins("TR-CHS-4")
def test_a_challenged_api_passes_through_the_solver() -> None:
    """A memberships call that gets a browser check is solved and answered with the real page."""
    captured: list[object] = []

    def fake_urlopen(req: object, *args: object, **kwargs: object) -> object:
        captured.append(req)
        if len(captured) == 1:
            raise _check(_MODULE.MEMBERSHIPS_URL)
        return _FakeResponse(b'{"data": []}')

    with mock.patch("urllib.request.urlopen", fake_urlopen), FakeSolver() as fake:
        router = ChallengeRouter(FakeContext(app_settings={"challenge_solver_url": fake.url}))
        result = _MODULE._get_json(
            _MODULE.MEMBERSHIPS_URL,
            "s",
            circuit=FakeCircuit(),
            router=router,
        )

    assert result == {"data": []}
    second_request = captured[1]
    assert second_request.get_header("Cookie") == "session_id=s; clearance=fake-clearance-value"
