"""Tests for My Literotica passing Literotica's browser check (TR-CHS-4).

``scan`` routes every request through the user's challenge solver: a Cloudflare browser check
is solved, or the scan fails with the solver sentence.
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

_MODULE = importlib.import_module("my_literotica.catalog")


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


def _fake_solution() -> Solution:
    """A solver answer whose clearance is the clearance the passing tests assert."""
    return Solution(
        url="https://www.literotica.com/",
        status=200,
        body="<pre>OK</pre>",
        clearance=Clearance(
            user_agent="Mozilla/5.0 (FakeSolver) Chrome/152.0",
            cookies=(
                ClearanceCookie("clearance", "fake-clearance-value", domain="literotica.com"),
            ),
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
    """Minimal stand-in for what ``urlopen`` (or ``opener.open``) returns."""

    def __init__(self, status: int, body: bytes) -> None:
        """Store the status and body this fake will serve."""
        self.status = status
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
def test_a_challenged_wall_without_a_solver_reads_the_sentence() -> None:
    """A wall read that gets a browser check and no solver reads the solver sentence."""
    with (
        mock.patch("urllib.request.urlopen", side_effect=_check(_MODULE.WALL_URL)),
        pytest.raises(ChallengeBlockedError) as caught,
    ):
        _MODULE.fetch_wall_page(
            "t", None, circuit=FakeCircuit(), router=ChallengeRouter(FakeContext())
        )

    assert str(caught.value) == (
        "literotica.com asks for a browser check — set a challenge solver in Settings → General"
    )


@pytest.mark.pins("TR-CHS-4")
def test_a_challenged_login_keeps_the_session_cookie(monkeypatch) -> None:
    """A login that gets a browser check returns the session and carries the clearance cookie."""
    import http.cookiejar as http_cookiejar_module

    captured: list[object] = []
    real_cookiejar = http_cookiejar_module.CookieJar
    real_cookie = http_cookiejar_module.Cookie

    def fake_cookiejar_factory(*args, **kwargs):
        jar = real_cookiejar()
        jar.set_cookie(
            real_cookie(
                version=0,
                name="sessionid",
                value="s3ss10n-value",
                port=None,
                port_specified=False,
                domain=".auth.literotica.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
        return jar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                captured.append(req)
                if len(captured) == 1:
                    raise _check(_MODULE.AUTH_LOGIN_URL)
                return _FakeResponse(200, b"OK")

        return FakeOpener()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    with FakeSolver() as fake:
        router = ChallengeRouter(FakeContext(app_settings={"challenge_solver_url": fake.url}))
        result = _MODULE.login("user", "pw", router=router)

    assert result == "s3ss10n-value"
    second_request = captured[1]
    assert second_request.get_header("Cookie") == "clearance=fake-clearance-value"


@pytest.mark.pins("TR-CHS-4")
def test_a_challenged_token_request_keeps_its_session(monkeypatch) -> None:
    """A token request that gets a browser check keeps the session cookie and adds the clearance."""
    captured: list[object] = []

    def fake_urlopen(req, *args, **kwargs):
        captured.append(req)
        if len(captured) == 1:
            raise _check(_MODULE.AUTH_CHECK_URL)
        return _FakeResponse(200, b"a.b.c")

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with FakeSolver() as fake:
        router = ChallengeRouter(FakeContext(app_settings={"challenge_solver_url": fake.url}))
        result = _MODULE.mint_token("s", circuit=FakeCircuit(), router=router)

    assert result == "a.b.c"
    second_request = captured[1]
    assert second_request.get_header("Cookie") == "sessionid=s; clearance=fake-clearance-value"
