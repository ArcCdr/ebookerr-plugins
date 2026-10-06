"""Tests for the My Literotica catalog plugin's circuit breaker."""

from __future__ import annotations

import importlib
import json
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

import pytest
from ebookerr_sdk.testing import FakeCircuit

_MODULE = importlib.import_module("my_literotica.catalog")


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
def test_a_literotica_wall_that_is_unreachable_is_probed_once_not_once_per_page() -> None:
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
        # Call fetch_wall_page 20 times
        token = "fake_token"
        for page_num in range(1, 21):
            last_id = str(page_num - 1) if page_num > 1 else None
            with pytest.raises(_MODULE.CatalogHostUnreachable):
                _MODULE.fetch_wall_page(token, last_id, circuit=circuit)

    # urlopen should have been called exactly once (on the first attempt)
    assert urlopen_calls == 1


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_makes_no_request_at_all() -> None:
    """When the breaker is open, fetch_wall_page raises without calling urlopen."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("should not reach"))
    circuit = FakeCircuit(open_keys={_MODULE.CIRCUIT_KEY})

    with mock.patch("urllib.request.urlopen", urlopen_mock):
        token = "fake_token"
        with pytest.raises(_MODULE.CatalogHostUnreachable):
            _MODULE.fetch_wall_page(token, None, circuit=circuit)

    # urlopen should never have been called
    urlopen_mock.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_a_successful_fetch_reports_ok() -> None:
    """A successful fetch_wall_page is reported to the breaker as a success."""
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

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        token = "fake_token"
        result = _MODULE.fetch_wall_page(token, None, circuit=circuit)

    assert result == response_data.get("data")

    # One guarded call, and it did not fail
    assert circuit.guarded == ["host:literotica.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_404_reports_ok_and_still_raises() -> None:
    """A 404 is reported as a success (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://literotica.com/api/3/activity/wall", 404, "Not Found", {}, None
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        token = "fake_token"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_wall_page(token, None, circuit=circuit)
        assert exc_info.value.code == 404

    # The host answered: one guarded call, not a failure
    assert circuit.guarded == ["host:literotica.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_401_reports_ok_and_still_raises() -> None:
    """A 401 is reported as a success (not a transport failure) but still raised."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://literotica.com/api/3/activity/wall", 401, "Unauthorized", {}, None
        )

    circuit = FakeCircuit()

    with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
        token = "fake_token"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _MODULE.fetch_wall_page(token, None, circuit=circuit)
        assert exc_info.value.code == 401

    # The host answered: one guarded call, not a failure
    assert circuit.guarded == ["host:literotica.com"]
    assert circuit.failed == []


@pytest.mark.pins("EXP-269")
def test_a_transport_error_reports_a_failure() -> None:
    """A TimeoutError is reported to the breaker as a failed call."""

    def mock_urlopen(*args: object, **kwargs: object) -> object:
        raise TimeoutError("timed out")

    circuit = FakeCircuit()

    with (
        mock.patch("urllib.request.urlopen", side_effect=mock_urlopen),
        mock.patch("time.sleep"),
    ):
        token = "fake_token"
        with pytest.raises(TimeoutError):
            _MODULE.fetch_wall_page(token, None, circuit=circuit)

    # The failure is reported once, against the host's key
    assert circuit.failed == ["host:literotica.com"]


@pytest.mark.pins("EXP-269")
def test_the_key_matches_the_sibling_catalog_exactly() -> None:
    """Both catalogs use the same circuit key to share one breaker."""
    sibling = importlib.import_module("literotica_stories.catalog")

    assert _MODULE.CIRCUIT_KEY == "host:literotica.com"
    assert sibling.CIRCUIT_KEY == _MODULE.CIRCUIT_KEY


@pytest.mark.pins("EXP-269")
@pytest.mark.parametrize(
    ("function_name", "args"),
    [
        ("fetch_wall_page", ("fake_token", None)),
        ("mint_token", ("fake_sessionid",)),
    ],
)
def test_every_urlopen_site_is_guarded(function_name: str, args: tuple[object, ...]) -> None:
    """Every urlopen call site is guarded by the circuit breaker."""
    urlopen_mock = mock.Mock(side_effect=urllib.error.URLError("should not reach"))
    circuit = FakeCircuit(open_keys={_MODULE.CIRCUIT_KEY})

    with (
        mock.patch("urllib.request.urlopen", urlopen_mock),
        pytest.raises(_MODULE.CatalogHostUnreachable),
    ):
        getattr(_MODULE, function_name)(*args, circuit=circuit)

    # urlopen should never have been called because the breaker was open
    urlopen_mock.assert_not_called()
