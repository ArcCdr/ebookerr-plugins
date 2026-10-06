"""Tests for the Patreon catalog plugin's circuit breaker."""

from __future__ import annotations

import importlib
import json
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

import pytest
from ebookerr_sdk.spi import SiteCredential
from ebookerr_sdk.testing import FakeCircuit, FakeCore, make_request, run_wire
from patreon_stories.plugin import PatreonStoriesPlugin

_MODULE = importlib.import_module("patreon_stories.catalog")

_CREDENTIAL = SiteCredential("cookie", "session_id", "c")
"""A stored patreon.com session cookie, as ``ctx.credentials`` answers it."""


def _json_response(payload: object) -> mock.MagicMock:
    """A stand-in for what ``urlopen`` returns: a context manager reading *payload* as JSON."""
    body = json.dumps(payload).encode("utf-8")
    return mock.MagicMock(
        __enter__=mock.MagicMock(
            return_value=mock.MagicMock(read=mock.MagicMock(return_value=body))
        ),
        __exit__=mock.MagicMock(return_value=None),
    )


class _OpeningCircuit(FakeCircuit):
    """A ``FakeCircuit`` whose breaker opens as soon as a guarded call fails, as the core's does."""

    @contextmanager
    def guard(self, key: str, *, label: str | None = None, notice: bool = True) -> Iterator[None]:
        """Guard a call; a block that raises opens *key* for every later ask."""
        with super().guard(key, label=label, notice=notice):
            try:
                yield
            except Exception:
                self.open_keys.add(key)
                raise


@pytest.mark.pins("EXP-269")
def test_a_patreon_host_that_is_unreachable_is_probed_once_not_once_per_campaign() -> None:
    """A dead host costs one probe, not one per campaign in a 20-campaign scan."""
    urlopen_calls = 0

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        nonlocal urlopen_calls
        urlopen_calls += 1
        raise urllib.error.URLError("down")

    # The first failed call opens the breaker; every later ask finds it open.
    circuit = _OpeningCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        # Try to fetch memberships and then 19 campaign posts
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE._get_json(_MODULE.MEMBERSHIPS_URL, "dummy_cookie", circuit=circuit)

        posts_url = "https://www.patreon.com/api/posts?filter[campaign_id]=1"
        for _ in range(19):
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE._get_json(posts_url, "dummy_cookie", circuit=circuit)

    # urlopen should have been called exactly once (on the first attempt)
    assert urlopen_calls == 1


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_makes_no_request_at_all() -> None:
    """When the breaker is open, _get_json raises without calling urlopen."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("should not reach"))
    circuit = FakeCircuit(open_keys={_MODULE.CIRCUIT_KEY})

    with mock.patch("urllib.request.urlopen", urlopen_mock):
        url = "https://www.patreon.com/api/current_user"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE._get_json(url, "dummy_cookie", circuit=circuit)

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_a_successful_fetch_reports_ok() -> None:
    """A successful fetch is reported to the breaker as a success."""
    response_data = {"data": {"id": "123", "type": "user"}}

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        return mock.MagicMock(
            read=mock.MagicMock(return_value=json.dumps(response_data).encode("utf-8")),
            __enter__=mock.MagicMock(
                return_value=mock.MagicMock(
                    read=mock.MagicMock(return_value=json.dumps(response_data).encode("utf-8"))
                )
            ),
            __exit__=mock.MagicMock(return_value=None),
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://www.patreon.com/api/current_user"
        result = _MODULE._get_json(url, "dummy_cookie", circuit=circuit)

    assert result == response_data

    # One guarded call, and it did not fail
    assert circuit.guarded == ["host:patreon.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_401_reports_ok_and_still_raises() -> None:
    """A 401 (expired session) is reported as a success but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://www.patreon.com/api/current_user", 401, "Unauthorized", {}, None
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://www.patreon.com/api/current_user"
        with pytest.raises(RuntimeError):
            _MODULE._get_json(url, "dummy_cookie", circuit=circuit)

    # The host answered: one guarded call, not a failure
    assert circuit.guarded == ["host:patreon.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_404_reports_ok_and_still_raises() -> None:
    """A 404 is reported as a success (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://www.patreon.com/api/posts", 404, "Not Found", {}, None
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://www.patreon.com/api/posts?filter[campaign_id]=1"
        with pytest.raises(RuntimeError):
            _MODULE._get_json(url, "dummy_cookie", circuit=circuit)

    # The host answered: one guarded call, not a failure
    assert circuit.guarded == ["host:patreon.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_transport_error_reports_a_failure() -> None:
    """A TimeoutError is reported to the breaker as a failed call."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise TimeoutError("timed out")

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://www.patreon.com/api/current_user"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE._get_json(url, "dummy_cookie", circuit=circuit)

    # The failure is reported once, against the host's key
    assert circuit.failed == ["host:patreon.com"]


@pytest.mark.pins("EXP-269")
def test_the_breaker_key_names_the_host() -> None:
    """The breaker is keyed 'host:patreon.com', never by the plugin's id."""
    response_data = {"data": {"id": "123", "type": "user"}}

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        return mock.MagicMock(
            read=mock.MagicMock(return_value=json.dumps(response_data).encode("utf-8")),
            __enter__=mock.MagicMock(
                return_value=mock.MagicMock(
                    read=mock.MagicMock(return_value=json.dumps(response_data).encode("utf-8"))
                )
            ),
            __exit__=mock.MagicMock(return_value=None),
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://www.patreon.com/api/current_user"
        _MODULE._get_json(url, "dummy_cookie", circuit=circuit)

    # Every guarded call names the host, never the plugin id
    assert _MODULE.CIRCUIT_KEY == "host:patreon.com"
    assert circuit.guarded == ["host:patreon.com"]


@pytest.mark.pins("EXP-269")
@pytest.mark.parametrize(
    "call_location",
    [
        "memberships",  # scan() -> _get_json(MEMBERSHIPS_URL)
        "posts",  # _scan_campaign() -> _get_json(posts_url)
    ],
)
def test_every_urlopen_site_is_guarded(call_location: str) -> None:
    """Every urlopen call site is guarded; an open breaker raises without calling urlopen."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("should not reach"))
    circuit = FakeCircuit(open_keys={_MODULE.CIRCUIT_KEY})

    with mock.patch("urllib.request.urlopen", urlopen_mock):
        if call_location == "memberships":
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE._get_json(_MODULE.MEMBERSHIPS_URL, "dummy_cookie", circuit=circuit)
        elif call_location == "posts":
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE._get_json(
                    _MODULE.POSTS_URL_TEMPLATE.format(campaign_id="1"),
                    "dummy_cookie",
                    circuit=circuit,
                )

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_the_scan_returns_empty_and_warns_when_the_host_is_down() -> None:
    """When the host is unreachable, scan() returns ok=true with empty stories and a warning."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("Host unreachable"))
    core = FakeCore(
        open_circuit_keys={"host:patreon.com"},
        credentials_by_host={"www.patreon.com": _CREDENTIAL},
    )

    with mock.patch("urllib.request.urlopen", urlopen_mock):
        terminal, _frames = run_wire(
            PatreonStoriesPlugin(), make_request("scan", settings={"recent_weeks": 4}), core=core
        )

    assert terminal["ok"] is True
    assert terminal["result"] == []
    assert len(terminal["logs"]) >= 1

    # Find the warning log
    warning_logs = [log for log in terminal["logs"] if log.get("level") == "warning"]
    assert len(warning_logs) >= 1
    assert "patreon.com is not reachable" in warning_logs[0]["message"]
    assert warning_logs[0]["message"] == (
        "patreon.com is not reachable, so this scan made no request."
        " It will be retried automatically."
    )

    # The breaker was open, so the host was never asked
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-227")
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        pytest.param(urllib.error.URLError("down"), "<urlopen error down>", id="a URL error"),
        pytest.param(TimeoutError("timed out"), "timed out", id="a timeout"),
    ],
)
def test_a_host_that_cannot_be_reached_fails_the_scan_while_its_breaker_is_closed(
    failure: Exception, reason: str
) -> None:
    """A transport failure its breaker has not yet turned into a held host answers ok=false.

    An empty scan means the source really has nothing (``EXP-073``); only an open breaker, the
    host already found unreachable, makes the quiet empty scan the test above pins.
    """
    core = FakeCore(credentials_by_host={"www.patreon.com": _CREDENTIAL})

    with mock.patch("urllib.request.urlopen", side_effect=failure):
        terminal, _frames = run_wire(PatreonStoriesPlugin(), make_request("scan"), core=core)

    assert terminal["ok"] is False
    assert terminal["error"] == f"patreon.com is not reachable: {reason}"


@pytest.mark.pins("EXP-269")
@pytest.mark.real_impl("ebookerr_sdk.host.HostContext")
@pytest.mark.parametrize(
    ("urlopen_outcome", "recorded_ok"),
    [
        pytest.param(
            {
                "return_value": _json_response(
                    {"data": {"id": "u1", "type": "user"}, "included": []}
                )
            },
            True,
            id="an answered call",
        ),
        pytest.param(
            {
                "side_effect": urllib.error.HTTPError(
                    "https://www.patreon.com/api/current_user", 401, "Unauthorized", {}, None
                )
            },
            True,
            id="a 401",
        ),
        pytest.param(
            {"side_effect": urllib.error.URLError("down")}, False, id="a transport failure"
        ),
    ],
)
def test_the_hosts_own_guard_reports_each_membership_outcome_to_the_core(
    urlopen_outcome: dict[str, object], recorded_ok: bool
) -> None:
    """Served by the real SDK host, a memberships call's outcome crosses the wire by host key."""
    core = FakeCore(credentials_by_host={"www.patreon.com": _CREDENTIAL})

    with mock.patch("urllib.request.urlopen", **urlopen_outcome):
        _terminal, frames = run_wire(PatreonStoriesPlugin(), make_request("scan"), core=core)

    records = [f for f in frames if f.get("op") == "circuit" and f.get("call") == "record"]
    assert [(f["key"], f["label"], f["ok"]) for f in records] == [
        ("host:patreon.com", "patreon.com", recorded_ok)
    ]
