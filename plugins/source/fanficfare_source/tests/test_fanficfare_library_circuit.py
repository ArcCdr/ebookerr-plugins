"""The library gateway's per-host circuit breaker (EXP-269)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
from ebookerr_sdk.spi import CircuitOpenError
from fanficfare import exceptions
from fanficfare_source import library
from fanficfare_source.library import FanFicFareLibraryGateway

PACKAGED_INI = Path(__file__).resolve().parents[1] / "fanficfare_source" / "personal.ini"
URL = "http://test1.com?sid=1"
KEY = "host:test1.com"
REFUSED = "test1.com is not reachable; retrying automatically"


class MockCircuit:
    """Mock circuit breaker for testing."""

    def __init__(self) -> None:
        """Initialize the mock circuit."""
        self.open_keys: set[str] = set()
        self.call_counts: dict[str, int] = {}
        self.failure_counts: dict[str, int] = {}

    def guard(self, key: str, *, label: str | None = None) -> MockGuardContext:
        """Return a context manager for guarding a call."""
        if key not in self.call_counts:
            self.call_counts[key] = 0
        if key not in self.failure_counts:
            self.failure_counts[key] = 0

        return MockGuardContext(self, key, label)

    def is_open(self, key: str) -> bool:
        """Check if a circuit is open."""
        return key in self.open_keys


class MockGuardContext:
    """Mock guard context manager."""

    def __init__(self, circuit: MockCircuit, key: str, label: str | None) -> None:
        """Initialize the guard context."""
        self.circuit = circuit
        self.key = key
        self.label = label
        self.circuit.call_counts[key] += 1

    def __enter__(self) -> None:
        """Enter the context; open breakers should raise before this."""
        if self.circuit.is_open(self.key):
            raise CircuitOpenError(self.key, self.label or self.key, datetime.now(UTC))
        return None

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        """Record success or failure on exit."""
        if exc_type is not None:
            self.circuit.failure_counts[self.key] += 1
            if self.circuit.failure_counts[self.key] >= 3:
                self.circuit.open_keys.add(self.key)
        else:
            self.circuit.failure_counts[self.key] = 0
        return False


def _gateway(circuit: MockCircuit | None) -> FanFicFareLibraryGateway:
    """A library gateway over the packaged personal.ini and *circuit*."""
    return FanFicFareLibraryGateway(PACKAGED_INI, circuit=circuit)


@pytest.mark.pins("EXP-269")
def test_a_fanficfare_host_that_is_unreachable_is_asked_once_not_once_per_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable host is asked three times; the other 17 stories are refused unasked."""
    calls: list[str] = []

    def down(config: Any, url: str) -> Any:
        """Fail as an unreachable host does."""
        calls.append(url)
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", down)
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    for story in range(1, 4):
        result = gateway.download(
            f"http://test1.com?sid={story}", work_dir=tmp_path, staged_filename=None
        )
        assert result.outcome == "failed"
        assert result.error == "the site could not be reached"
    assert circuit.is_open(KEY)
    for story in range(4, 21):
        result = gateway.download(
            f"http://test1.com?sid={story}", work_dir=tmp_path, staged_filename=None
        )
        assert result.error == REFUSED
    assert len(calls) == 3


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_calls_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the host's breaker open neither a download nor a metadata fetch reaches FanFicFare."""
    calls: list[str] = []

    def down(config: Any, url: str) -> Any:
        """Fail as an unreachable host does."""
        calls.append(url)
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", down)
    circuit = MockCircuit()
    circuit.open_keys.add(KEY)
    gateway = _gateway(circuit)
    result = gateway.download(URL, work_dir=tmp_path, staged_filename=None)
    assert result.error == REFUSED
    assert gateway.fetch_metadata(URL) is None
    assert calls == []


@pytest.mark.pins("EXP-269")
def test_a_connection_error_trips_the_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three connection errors in a row open the host's breaker."""

    def down(config: Any, url: str) -> Any:
        """Fail as an unreachable host does."""
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", down)
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    for _ in range(3):
        gateway.download(URL, work_dir=tmp_path, staged_filename=None)
    assert circuit.is_open(KEY)


@pytest.mark.pins("EXP-269")
def test_an_http_503_trips_the_breaker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three HTTP 503 answers in a row open the host's breaker."""

    def unavailable(config: Any, url: str) -> Any:
        """Fail as an overloaded host does."""
        raise exceptions.HTTPErrorFFF(url, 503, "Service Unavailable")

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", unavailable)
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    for _ in range(3):
        result = gateway.download(URL, work_dir=tmp_path, staged_filename=None)
        assert result.error == "the site answered HTTP 503"
    assert circuit.is_open(KEY)


@pytest.mark.pins("EXP-269")
def test_a_story_not_found_never_trips_the_breaker(tmp_path: Path) -> None:
    """A story the site does not have says nothing about the site's health."""
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    for _ in range(5):
        result = gateway.download(
            "http://test1.com?sid=666", work_dir=tmp_path, staged_filename=None
        )
        assert result.error == "story not found: http://test1.com?sid=666"
    assert not circuit.is_open(KEY)
    assert circuit.failure_counts.get(KEY, 0) == 0


@pytest.mark.pins("EXP-269")
def test_an_unrecognised_file_never_trips_the_breaker(tmp_path: Path) -> None:
    """A staged file FanFicFare cannot read is not a failure of the site."""
    (tmp_path / "x.epub").write_bytes(b"not a zip")
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    for _ in range(3):
        result = gateway.download(URL, work_dir=tmp_path, staged_filename="x.epub")
        assert result.outcome == "unrecognised"
    assert not circuit.is_open(KEY)
    assert circuit.failure_counts.get(KEY, 0) == 0


@pytest.mark.pins("EXP-269")
def test_a_successful_download_resets_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed download clears the host's failure count, so the breaker stays shut."""
    original = library.adapters.getAdapter
    plan = ["down", "down", "real", "down"]

    def scripted(config: Any, url: str) -> Any:
        """Fail or answer for real, as the plan says, one step per call."""
        if plan.pop(0) == "down":
            raise requests.exceptions.ConnectionError()
        return original(config, url)

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", scripted)
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    outcomes = [
        gateway.download(URL, work_dir=tmp_path, staged_filename=None).outcome for _ in range(4)
    ]
    assert outcomes == ["failed", "failed", "created", "failed"]
    assert not circuit.is_open(KEY)
    assert circuit.failure_counts[KEY] == 1


@pytest.mark.pins("EXP-269")
def test_the_metadata_and_download_paths_share_one_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata fetches and downloads of one host count towards the same breaker."""
    calls: list[str] = []

    def down(config: Any, url: str) -> Any:
        """Fail as an unreachable host does."""
        calls.append(url)
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", down)
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    assert gateway.fetch_metadata(URL) is None
    assert gateway.fetch_metadata(URL) is None
    assert gateway.download(URL, work_dir=tmp_path, staged_filename=None).outcome == "failed"
    assert circuit.is_open(KEY)
    assert gateway.fetch_metadata(URL) is None
    assert len(calls) == 3


@pytest.mark.pins("EXP-269")
def test_two_hosts_keep_two_breakers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One host going down leaves every other host's breaker alone."""
    original = library.adapters.getAdapter

    def test1_down(config: Any, url: str) -> Any:
        """Fail for test1.com; answer for real for any other host."""
        if "test1.com" in url:
            raise requests.exceptions.ConnectionError()
        return original(config, url)

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", test1_down)
    circuit = MockCircuit()
    gateway = _gateway(circuit)
    for _ in range(3):
        assert gateway.download(URL, work_dir=tmp_path, staged_filename=None).outcome == "failed"
    assert circuit.is_open(KEY)
    for _ in range(3):
        result = gateway.download("http://test2.com?sid=1", work_dir=tmp_path, staged_filename=None)
        assert result.outcome == "created"
    assert not circuit.is_open("host:test2.com")


@pytest.mark.pins("EXP-269")
def test_the_gateway_is_unguarded_without_a_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no circuit given every call reaches FanFicFare, however many fail."""
    calls: list[str] = []

    def down(config: Any, url: str) -> Any:
        """Fail as an unreachable host does."""
        calls.append(url)
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr("fanficfare_source.library.adapters.getAdapter", down)
    gateway = _gateway(None)
    for _ in range(20):
        assert gateway.download(URL, work_dir=tmp_path, staged_filename=None).outcome == "failed"
    assert len(calls) == 20


@pytest.mark.pins("EXP-269")
def test_the_skip_is_logged_at_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A call refused by an open breaker leaves one INFO line naming the host and the URL."""
    circuit = MockCircuit()
    circuit.open_keys.add(KEY)
    with caplog.at_level(logging.INFO, logger="fanficfare_source.library"):
        _gateway(circuit).download(URL, work_dir=tmp_path, staged_filename=None)
    assert (
        logging.INFO,
        "FanFicFare skipped: test1.com is in a failure back-off (url=http://test1.com?sid=1)",
    ) in [(record.levelno, record.getMessage()) for record in caplog.records]
