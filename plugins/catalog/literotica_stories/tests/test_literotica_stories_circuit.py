"""Tests for the Literotica catalog plugin's circuit breaker."""

from __future__ import annotations

import importlib
import json
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

import pytest
from ebookerr_sdk.testing import FakeCircuit

_MODULE = importlib.import_module("literotica_stories.catalog")


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
def test_a_literotica_host_that_is_unreachable_is_probed_once_not_once_per_page() -> None:
    """A dead host costs one probe, not one per page in a 20-page scan."""
    urlopen_calls = 0

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        nonlocal urlopen_calls
        urlopen_calls += 1
        raise urllib.error.URLError("down")

    circuit = _OpeningCircuit()

    with (
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
        mock.patch("time.sleep"),
    ):
        # Call fetch_json 20 times for distinct page URLs
        for page_num in range(1, 21):
            url = (
                f"https://literotica.com/api/3/search/stories?params=%7B%22page%22%3A{page_num}%7D"
            )
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE.fetch_json(url, circuit=circuit)

    # urlopen should have been called exactly once (on the first attempt)
    assert urlopen_calls == 1


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_makes_no_request_at_all() -> None:
    """When the breaker is open, fetch_json raises without calling urlopen."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("should not reach"))
    circuit = FakeCircuit(open_keys={_MODULE.CIRCUIT_KEY})

    with mock.patch("urllib.request.urlopen", urlopen_mock):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE.fetch_json(url, circuit=circuit)

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_a_successful_fetch_reports_ok() -> None:
    """A successful fetch is reported to the breaker as a success."""
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

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        result = _MODULE.fetch_json(url, circuit=circuit)

    assert result == response_data

    # One guarded call, and it did not fail
    assert circuit.guarded == ["host:literotica.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_404_reports_ok_and_still_raises() -> None:
    """A 404 is reported as a success (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://literotica.com/api/3/search/stories", 404, "Not Found", {}, None
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_json(url, circuit=circuit)
        assert exc_info.value.code == 404

    # The host answered: one guarded call, not a failure
    assert circuit.guarded == ["host:literotica.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_401_reports_ok_and_still_raises() -> None:
    """A 401 is reported as a success (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://literotica.com/api/3/search/stories", 401, "Unauthorized", {}, None
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_json(url, circuit=circuit)
        assert exc_info.value.code == 401

    # The host answered: one guarded call, not a failure
    assert circuit.guarded == ["host:literotica.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_500_reports_ok_and_retries_once() -> None:
    """A 500 is reported as a success and retried; second attempt succeeds."""
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

    circuit = FakeCircuit()

    with (
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
        mock.patch("time.sleep"),
    ):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        result = _MODULE.fetch_json(url, circuit=circuit)

    assert result == response_data
    assert call_count == 2

    # Both outcomes (the 500 and the success) were reported, neither as a failure
    assert circuit.guarded == ["host:literotica.com", "host:literotica.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_transport_error_reports_a_failure() -> None:
    """A TimeoutError is reported to the breaker as a failed call (on both attempts)."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise TimeoutError("timed out")

    circuit = FakeCircuit()

    with (
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
        mock.patch("time.sleep"),
    ):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        with pytest.raises(TimeoutError):
            _MODULE.fetch_json(url, circuit=circuit)

    # The call is retried once, and each attempt's failure is reported
    assert circuit.failed == ["host:literotica.com", "host:literotica.com"]


@pytest.mark.pins("EXP-269")
def test_the_breaker_key_names_the_host() -> None:
    """The breaker is keyed 'host:literotica.com', never by the plugin's id."""
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

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        url = "https://literotica.com/api/3/search/stories?params=%7B%7D"
        _MODULE.fetch_json(url, circuit=circuit)

    # Every guarded call names the host, never the plugin id
    assert circuit.guarded == ["host:literotica.com"]
