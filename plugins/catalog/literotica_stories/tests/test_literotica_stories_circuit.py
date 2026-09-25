"""Tests for the Literotica catalog plugin's circuit breaker."""

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
_SPEC = importlib.util.spec_from_file_location("literotica_stories_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["literotica_stories_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)


@pytest.mark.pins("EXP-269")
def test_a_literotica_host_that_is_unreachable_is_probed_once_not_once_per_page() -> None:
    """A dead host costs one probe, not one per page in a 20-page scan."""
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
        mock.patch("time.sleep"),
    ):
        # Call fetch_json 20 times for distinct page URLs
        for page_num in range(1, 21):
            url = (
                f"https://literotica.com/api/3/search/stories?params=%7B%22page%22%3A{page_num}%7D"
            )
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE.fetch_json(url)

    # urlopen should have been called exactly once (on the first attempt)
    assert urlopen_calls == 1


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_makes_no_request_at_all() -> None:
    """When the breaker is open, fetch_json raises without calling urlopen."""
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
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE.fetch_json(url)

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_a_successful_fetch_reports_ok() -> None:
    """A successful fetch reports ok=true to the circuit channel."""
    response_data = {"data": [{"id": 1, "title": "test"}]}

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
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        result = _MODULE.fetch_json(url)

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
    assert frame1["key"] == "host:literotica.com"

    # Second frame should be record with ok=true
    assert frame2["op"] == "circuit"
    assert frame2["call"] == "record"
    assert frame2["ok"] is True
    assert frame2["key"] == "host:literotica.com"


@pytest.mark.pins("EXP-269")
def test_a_404_reports_ok_and_still_raises() -> None:
    """A 404 is reported as ok=true (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://literotica.com/api/3/search/stories", 404, "Not Found", {}, None
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
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_json(url)
        assert exc_info.value.code == 404

    # Check that record reported ok=true
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    frame2 = json.loads(lines[1])
    assert frame2["call"] == "record"
    assert frame2["ok"] is True


@pytest.mark.pins("EXP-269")
def test_a_401_reports_ok_and_still_raises() -> None:
    """A 401 is reported as ok=true (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://literotica.com/api/3/search/stories", 401, "Unauthorized", {}, None
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
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_json(url)
        assert exc_info.value.code == 401

    # Check that record reported ok=true
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    frame2 = json.loads(lines[1])
    assert frame2["call"] == "record"
    assert frame2["ok"] is True


@pytest.mark.pins("EXP-269")
def test_a_500_reports_ok_and_retries_once() -> None:
    """A 500 is reported as ok=true and retried; second attempt succeeds."""
    call_count = 0
    response_data = {"data": [{"id": 1}]}

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise urllib.error.HTTPError(
                "https://literotica.com/api/3/search/stories",
                500,
                "Internal Server Error",
                {},
                None,
            )
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
        '{"circuit": {"open": false}}\n',  # first record response (for 500)
        '{"circuit": {"open": false}}\n',  # second record response (for success)
    ]
    stdin_iter = iter(stdin_responses)

    stdout_capture = io.StringIO()

    with (
        mock.patch("sys.stdout", stdout_capture),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
        mock.patch("time.sleep"),
    ):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        result = _MODULE.fetch_json(url)

    assert result == response_data
    assert call_count == 2

    # Check frames
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    # Should have is_open, record (500), record (success)
    assert len(lines) >= 3
    frame2 = json.loads(lines[1])
    frame3 = json.loads(lines[2])
    assert frame2["call"] == "record"
    assert frame2["ok"] is True
    assert frame3["call"] == "record"
    assert frame3["ok"] is True


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
        mock.patch("time.sleep"),
    ):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(TimeoutError):
            _MODULE.fetch_json(url)

    # Check that record reported ok=false
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    frame2 = json.loads(lines[1])
    assert frame2["call"] == "record"
    assert frame2["ok"] is False


@pytest.mark.pins("EXP-269")
def test_a_broken_channel_never_blocks_a_scan() -> None:
    """When stdin returns EOF, _circuit returns False and fetch proceeds."""
    response_data = {"data": [{"id": 1}]}

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
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        result = _MODULE.fetch_json(url)

    # Should have proceeded despite broken channel
    assert result == response_data


@pytest.mark.pins("EXP-269")
def test_a_malformed_reply_never_blocks_a_scan() -> None:
    """When stdin returns invalid JSON, _circuit returns False and fetch proceeds."""
    response_data = {"data": [{"id": 1}]}

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

    # stdin returns invalid JSON for is_open, returns invalid JSON for record
    stdin_responses = ["not json\n", "also not json\n"]
    stdin_iter = iter(stdin_responses)

    with (
        mock.patch("sys.stdout", new_callable=io.StringIO),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
    ):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        result = _MODULE.fetch_json(url)

    # Should have proceeded despite malformed response
    assert result == response_data


@pytest.mark.pins("EXP-269")
def test_the_frames_name_the_host_not_the_plugin() -> None:
    """Every frame written to stdout uses key='host:literotica.com', never the plugin id."""
    response_data = {"data": [{"id": 1}]}

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
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        _MODULE.fetch_json(url)

    # Check that all frames name the host correctly
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    for line in lines:
        frame = json.loads(line)
        assert frame.get("key") == "host:literotica.com"
        # Ensure no mention of plugin id like "literotica_stories"
        assert "literotica_stories" not in frame.get("key", "")
