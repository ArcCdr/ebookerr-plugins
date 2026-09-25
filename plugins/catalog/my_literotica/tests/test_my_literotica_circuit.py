"""Tests for the My Literotica catalog plugin's circuit breaker."""

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
_SPEC = importlib.util.spec_from_file_location("my_literotica_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["my_literotica_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)


@pytest.mark.pins("EXP-269")
def test_a_literotica_wall_that_is_unreachable_is_probed_once_not_once_per_page() -> None:
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
        # Call fetch_wall_page 20 times
        token = "fake_token"
        for page_num in range(1, 21):
            last_id = str(page_num - 1) if page_num > 1 else None
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE.fetch_wall_page(token, last_id)

    # urlopen should have been called exactly once (on the first attempt)
    assert urlopen_calls == 1


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_makes_no_request_at_all() -> None:
    """When the breaker is open, fetch_wall_page raises without calling urlopen."""
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
        token = "fake_token"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE.fetch_wall_page(token, None)

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_a_successful_fetch_reports_ok() -> None:
    """A successful fetch_wall_page reports ok=true to the circuit channel."""
    response_data = {"data": [{"id": 1, "action": "published-story"}]}

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
        token = "fake_token"
        result = _MODULE.fetch_wall_page(token, None)

    assert result == response_data.get("data")

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
            "https://literotica.com/api/3/activity/wall", 404, "Not Found", {}, None
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
        token = "fake_token"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_wall_page(token, None)
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
            "https://literotica.com/api/3/activity/wall", 401, "Unauthorized", {}, None
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
        token = "fake_token"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_wall_page(token, None)
        assert exc_info.value.code == 401

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
        mock.patch("time.sleep"),
    ):
        token = "fake_token"
        with pytest.raises(TimeoutError):
            _MODULE.fetch_wall_page(token, None)

    # Check that record reported ok=false
    output = stdout_capture.getvalue()
    lines = output.strip().split("\n")
    frame2 = json.loads(lines[1])
    assert frame2["call"] == "record"
    assert frame2["ok"] is False


@pytest.mark.pins("EXP-269")
def test_a_broken_channel_never_blocks_a_scan() -> None:
    """When stdin returns EOF, _circuit returns False and fetch proceeds."""
    response_data = {"data": [{"id": 1, "action": "published-story"}]}

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
        token = "fake_token"
        result = _MODULE.fetch_wall_page(token, None)

    # Should have proceeded despite broken channel
    assert result == response_data.get("data")


@pytest.mark.pins("EXP-269")
def test_the_key_matches_the_sibling_catalog_exactly() -> None:
    """Both catalogs use the same circuit key to share one breaker."""
    # Read both entrypoint sources
    my_literotica_path = Path(__file__).resolve().parents[1] / "entrypoint.py"
    literotica_stories_path = (
        Path(__file__).resolve().parents[2] / "literotica_stories" / "entrypoint.py"
    )

    my_lit_content = my_literotica_path.read_text()
    stories_content = literotica_stories_path.read_text()

    # Both must contain this exact key
    assert 'CIRCUIT_KEY = "host:literotica.com"' in my_lit_content
    assert 'CIRCUIT_KEY = "host:literotica.com"' in stories_content


@pytest.mark.pins("EXP-269")
@pytest.mark.parametrize(
    ("function_name", "url_fragment"),
    [
        ("fetch_wall_page", "/api/3/activity/wall"),
        ("mint_token", "/check"),
    ],
)
def test_every_urlopen_site_is_guarded(function_name: str, url_fragment: str) -> None:
    """Every urlopen call site is guarded by the circuit breaker."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("should not reach"))

    stdin_responses = [
        '{"circuit": {"open": true}}\n',  # is_open response (breaker open)
    ]
    stdin_iter = iter(stdin_responses)

    with (
        mock.patch("sys.stdout", new_callable=io.StringIO),
        mock.patch("sys.stdin.readline", side_effect=lambda: next(stdin_iter, "")),
        mock.patch("urllib.request.urlopen", urlopen_mock),
    ):
        if function_name == "fetch_wall_page":
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE.fetch_wall_page("fake_token", None)
        elif function_name == "mint_token":
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE.mint_token("fake_sessionid")

    # urlopen should never have been called because the breaker was open
    urlopen_mock.assert_not_called()
