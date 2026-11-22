"""Tests for the Literotica catalog passing Literotica's browser check (TR-CHS-4).

``scan`` routes every request through the user's challenge solver: a Cloudflare browser check
is solved, or the scan fails with the solver sentence.
"""

from __future__ import annotations

import email.message
import importlib
import json
from typing import Any
from unittest import mock

import pytest
import urllib.error
from ebookerr_sdk.challenge.solver import Clearance, ClearanceCookie, Solution
from ebookerr_sdk.testing import FakeContext
from literotica_stories.plugin import LiteroticaStoriesPlugin

_MODULE = importlib.import_module("literotica_stories.catalog")

_SEARCH_URL = "https://search.literotica.com/?query=a"
_ONE_PAGE = {"data": [{"url": "https://www.literotica.com/story/1"}]}


def _json_response(payload: object) -> mock.MagicMock:
    """A stand-in for what ``urlopen`` returns: a context manager reading *payload* as JSON."""
    body = json.dumps(payload).encode("utf-8")
    return mock.MagicMock(
        __enter__=mock.MagicMock(
            return_value=mock.MagicMock(read=mock.MagicMock(return_value=body))
        ),
        __exit__=mock.MagicMock(return_value=None),
    )


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
    """A solver answer whose clearance is the clearance the passing test asserts."""
    return Solution(
        url=_SEARCH_URL,
        status=200,
        body="<pre>{\"data\": []}</pre>",
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


@pytest.mark.pins("TR-CHS-4")
def test_a_challenged_search_without_a_solver_fails_the_scan_with_the_sentence() -> None:
    """A search that gets a browser check and no solver fails the scan with the solver sentence."""
    settings = {"search_urls": [_SEARCH_URL], "max_pages": 1}

    with (
        mock.patch("urllib.request.urlopen", side_effect=_check(_SEARCH_URL)),
        mock.patch("literotica_stories.catalog.time.sleep"),
    ):
        with pytest.raises(RuntimeError) as caught:
            _MODULE.scan(settings, FakeContext())

    message = str(caught.value)
    assert (
        "literotica.com asks for a browser check — set a challenge solver in Settings → General"
        in message
    )
    assert "403" not in message


@pytest.mark.pins("TR-CHS-4")
def test_a_challenged_search_passes_through_the_solver() -> None:
    """A search that gets a browser check passes it through the solver and returns its stories."""
    settings = {"search_urls": [_SEARCH_URL], "max_pages": 1}
    urlopen_patcher = mock.patch(
        "urllib.request.urlopen",
        side_effect=[_check(_SEARCH_URL), _json_response(_ONE_PAGE)],
    )
    with FakeSolver() as fake:
        urlopen_mock = urlopen_patcher.start()
        with mock.patch("literotica_stories.catalog.time.sleep"):
            ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
            stories, _logs = _MODULE.scan(settings, ctx)
        urlopen_patcher.stop()

    assert len(stories) == 1
    second_request = urlopen_mock.call_args_list[1].args[0]
    assert second_request.get_header("Cookie") == "clearance=fake-clearance-value"
    assert second_request.get_header("User-agent") == "Mozilla/5.0 (FakeSolver) Chrome/152.0"
