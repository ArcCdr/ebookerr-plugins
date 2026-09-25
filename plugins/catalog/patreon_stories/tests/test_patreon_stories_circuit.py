"""Tests for the Patreon catalog plugin's circuit breaker."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

# Dynamically import entrypoint module
_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "entrypoint.py"
_SPEC = importlib.util.spec_from_file_location("patreon_stories_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["patreon_stories_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)


@pytest.mark.pins("EXP-269")
def test_a_patreon_host_that_is_unreachable_is_probed_once_not_once_per_campaign() -> None:
    """A dead host costs one probe, not one per campaign in a 20-campaign scan."""
    urlopen_calls = 0

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        nonlocal urlopen_calls
        urlopen_calls += 1
        raise urllib.error.URLError("down")

    # First call: is_open (returns false), record (opens breaker)
    # Subsequent calls: is_open (returns true from open breaker)
    stdin_responses = ['{"circuit": {"open": false}}\n', '{"circuit": {"open": true}}\n'] + [
        '{"circuit": {"open": true}}\n'
    ] * 19
    stdin_iter = iter(stdin_responses)

    with (
        mock.patch("sys.stdout", new_callable=io.StringIO),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        # Try to fetch memberships and then 19 campaign posts
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE._get_json(_MODULE.MEMBERSHIPS_URL, "dummy_cookie")

        posts_url = "https://www.patreon.com/api/posts?filter[campaign_id]=1"
        for _ in range(19):
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE._get_json(posts_url, "dummy_cookie")

    # urlopen should have been called exactly once (on the first attempt)
    assert urlopen_calls == 1


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_makes_no_request_at_all() -> None:
    """When the breaker is open, _get_json raises without calling urlopen."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("should not reach"))

    stdin_responses = [
        '{"circuit": {"open": true}}\n',  # is_open response
    ]
    stdin_iter = iter(stdin_responses)

    with (
        mock.patch("sys.stdout", new_callable=io.StringIO),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", urlopen_mock),
    ):
        url = "https://www.patreon.com/api/current_user"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE._get_json(url, "dummy_cookie")

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_a_successful_fetch_reports_ok() -> None:
    """A successful fetch reports ok=true to the circuit channel."""
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

    stdin_responses = [
        '{"circuit": {"open": false}}\n',  # is_open response
        '{"circuit": {"open": false}}\n',  # record response
    ]
    stdin_iter = iter(stdin_responses)

    stdout_capture = io.StringIO()

    with (
        mock.patch("sys.stdout", stdout_capture),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        url = "https://www.patreon.com/api/current_user"
        result = _MODULE._get_json(url, "dummy_cookie")

    assert result == response_data

    # Check that frames were written to stdout
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    assert len(lines) >= 2

    # Parse frames
    frame1 = json.loads(lines[0])
    frame2 = json.loads(lines[1])

    # First frame should be is_open
    assert frame1["op"] == "circuit"
    assert frame1["call"] == "is_open"
    assert frame1["key"] == "host:patreon.com"

    # Second frame should be record with ok=true
    assert frame2["op"] == "circuit"
    assert frame2["call"] == "record"
    assert frame2["ok"] is True
    assert frame2["key"] == "host:patreon.com"


@pytest.mark.pins("EXP-269")
def test_a_401_reports_ok_and_still_raises() -> None:
    """A 401 (expired session) is reported as ok=true but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://www.patreon.com/api/current_user", 401, "Unauthorized", {}, None
        )

    stdin_responses = [
        '{"circuit": {"open": false}}\n',  # is_open response
        '{"circuit": {"open": false}}\n',  # record response
    ]
    stdin_iter = iter(stdin_responses)

    stdout_capture = io.StringIO()

    with (
        mock.patch("sys.stdout", stdout_capture),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        url = "https://www.patreon.com/api/current_user"
        with pytest.raises(RuntimeError):
            _MODULE._get_json(url, "dummy_cookie")

    # Check that record reported ok=true
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    frame2 = json.loads(lines[1])
    assert frame2["call"] == "record"
    assert frame2["ok"] is True


@pytest.mark.pins("EXP-269")
def test_a_404_reports_ok_and_still_raises() -> None:
    """A 404 is reported as ok=true (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://www.patreon.com/api/posts", 404, "Not Found", {}, None
        )

    stdin_responses = [
        '{"circuit": {"open": false}}\n',  # is_open response
        '{"circuit": {"open": false}}\n',  # record response
    ]
    stdin_iter = iter(stdin_responses)

    stdout_capture = io.StringIO()

    with (
        mock.patch("sys.stdout", stdout_capture),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        url = "https://www.patreon.com/api/posts?filter[campaign_id]=1"
        with pytest.raises(RuntimeError):
            _MODULE._get_json(url, "dummy_cookie")

    # Check that record reported ok=true
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    frame2 = json.loads(lines[1])
    assert frame2["call"] == "record"
    assert frame2["ok"] is True


@pytest.mark.pins("EXP-269")
def test_a_transport_error_reports_a_failure() -> None:
    """A TimeoutError is reported as ok=false to the circuit."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise TimeoutError("timed out")

    stdin_responses = [
        '{"circuit": {"open": false}}\n',  # is_open response
        '{"circuit": {"open": false}}\n',  # record response (ok=false)
    ]
    stdin_iter = iter(stdin_responses)

    stdout_capture = io.StringIO()

    with (
        mock.patch("sys.stdout", stdout_capture),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        url = "https://www.patreon.com/api/current_user"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE._get_json(url, "dummy_cookie")

    # Check that record reported ok=false
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    frame2 = json.loads(lines[1])
    assert frame2["call"] == "record"
    assert frame2["ok"] is False


@pytest.mark.pins("EXP-269")
def test_a_broken_channel_never_blocks_a_scan() -> None:
    """When stdin returns EOF, _circuit returns False and fetch proceeds."""
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

    # stdin returns EOF for is_open, returns EOF for record
    stdin_responses = ["", ""]
    stdin_iter = iter(stdin_responses)

    with (
        mock.patch("sys.stdout", new_callable=io.StringIO),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        url = "https://www.patreon.com/api/current_user"
        result = _MODULE._get_json(url, "dummy_cookie")

    # Should have proceeded despite broken channel
    assert result == response_data


@pytest.mark.pins("EXP-269")
def test_the_frames_name_the_host_not_the_plugin() -> None:
    """Every frame written to stdout uses key='host:patreon.com', never the plugin id."""
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

    stdin_responses = [
        '{"circuit": {"open": false}}\n',  # is_open response
        '{"circuit": {"open": false}}\n',  # record response
    ]
    stdin_iter = iter(stdin_responses)

    stdout_capture = io.StringIO()

    with (
        mock.patch("sys.stdout", stdout_capture),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        url = "https://www.patreon.com/api/current_user"
        _MODULE._get_json(url, "dummy_cookie")

    # Check that all frames name the host correctly
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    for line in lines:
        frame = json.loads(line)
        assert frame.get("key") == "host:patreon.com"
        # Ensure no mention of plugin id like "patreon_stories"
        assert "patreon_stories" not in frame.get("key", "")


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

    stdin_responses = [
        '{"circuit": {"open": true}}\n',  # is_open response
    ]
    stdin_iter = iter(stdin_responses)

    with (
        mock.patch("sys.stdout", new_callable=io.StringIO),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", urlopen_mock),
    ):
        if call_location == "memberships":
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE._get_json(_MODULE.MEMBERSHIPS_URL, "dummy_cookie")
        elif call_location == "posts":
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE._get_json(
                    _MODULE.POSTS_URL_TEMPLATE.format(campaign_id="1"), "dummy_cookie"
                )

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_the_scan_returns_empty_and_warns_when_the_host_is_down() -> None:
    """When the host is unreachable, scan() returns ok=true with empty stories and a warning."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.URLError("Host unreachable")

    request_json = json.dumps(
        {
            "op": "scan",
            "request": {"auth": {"patreon.com": {"value": "dummy_session_cookie"}}, "settings": {}},
        }
    )

    stdout_capture = io.StringIO()
    stdin_responses = [
        '{"circuit": {"open": true}}\n',  # is_open response
    ]
    stdin_iter = iter(stdin_responses)

    with (
        mock.patch("sys.stdout", stdout_capture),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("builtins.input", return_value=request_json),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        _MODULE.main()

    # stdout contains circuit frames and the final response JSON, each on its own line
    output_str = stdout_capture.getvalue()
    lines = output_str.strip().split("\n")
    # The last line should be the response
    response = json.loads(lines[-1])

    assert response["ok"] is True
    assert response["result"] == []
    assert len(response["logs"]) >= 1

    # Find the warning log
    warning_logs = [log for log in response["logs"] if log.get("level") == "warning"]
    assert len(warning_logs) >= 1
    assert "patreon.com is not reachable" in warning_logs[0]["message"]
