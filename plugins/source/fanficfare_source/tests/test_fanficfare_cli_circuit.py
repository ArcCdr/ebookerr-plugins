"""Test FanFicFare gateway circuit breaker integration (EXP-269)."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest
from ebookerr_sdk.spi import CircuitOpenError
from fanficfare_source.cli import FanFicFareCliGateway


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
            raise CircuitOpenError(self.key, self.label or self.key, MagicMock())
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


@pytest.mark.pins("EXP-269")
def test_a_fanficfare_host_that_is_unreachable_is_run_once_not_once_per_story(
    tmp_path: Any,
) -> None:
    """Unreachable host is tried 3 times, then remaining 17 URLs are refused without spawning."""
    call_count = [0]
    circuit = MockCircuit()

    def failing_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Raise TimeoutExpired on every call."""
        call_count[0] += 1
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=failing_runner, circuit=circuit)

    # Call download 20 times with the same host
    for i in range(20):
        result = gateway.download(
            f"https://a.example/story{i}",
            work_dir=tmp_path,
        )
        assert result.ok is False

    # The runner should have been called exactly 3 times (until breaker opens)
    assert call_count[0] == 3
    # The circuit should be open
    assert circuit.is_open("host:a.example") is True


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_spawns_nothing(tmp_path: Any) -> None:
    """With breaker open, runner is never called."""
    call_count = [0]
    circuit = MockCircuit()
    circuit.open_keys.add("host:a.example")

    def failing_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """This should never be called."""
        call_count[0] += 1
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=failing_runner, circuit=circuit)

    # download returns failure without spawning
    result = gateway.download("https://a.example/story", work_dir=tmp_path)
    assert result.ok is False
    assert call_count[0] == 0

    # fetch_metadata returns None without spawning
    meta = gateway.fetch_metadata("https://a.example/story")
    assert meta is None
    assert call_count[0] == 0


@pytest.mark.pins("EXP-269")
def test_a_timeout_trips_the_breaker(tmp_path: Any) -> None:
    """Three timeouts open the breaker."""
    circuit = MockCircuit()
    call_count = [0]

    def failing_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Raise TimeoutExpired."""
        call_count[0] += 1
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=failing_runner, circuit=circuit)

    # Three timeouts
    for _ in range(3):
        result = gateway.download("https://a.example/story", work_dir=tmp_path)
        assert result.ok is False

    # Breaker should now be open
    assert circuit.is_open("host:a.example") is True


@pytest.mark.pins("EXP-269")
def test_an_os_error_trips_the_breaker(tmp_path: Any) -> None:
    """Three OSErrors open the breaker."""
    circuit = MockCircuit()
    call_count = [0]

    def failing_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Raise OSError."""
        call_count[0] += 1
        raise OSError("No such file or directory")

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=failing_runner, circuit=circuit)

    # Three OSErrors
    for _ in range(3):
        result = gateway.download("https://a.example/story", work_dir=tmp_path)
        assert result.ok is False

    # Breaker should now be open
    assert circuit.is_open("host:a.example") is True


@pytest.mark.pins("EXP-269")
def test_a_story_not_found_never_trips_the_breaker(tmp_path: Any) -> None:
    """A runner that reports not-found doesn't open breaker."""
    circuit = MockCircuit()

    def not_found_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Return success with no metadata (story not found)."""
        return MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=not_found_runner, circuit=circuit)

    # Run 10 times
    for _ in range(10):
        result = gateway.download("https://a.example/story", work_dir=tmp_path)
        assert result.ok is False

    # Breaker should still be closed
    assert circuit.is_open("host:a.example") is False


@pytest.mark.pins("EXP-269")
def test_no_new_chapters_never_trips_the_breaker(tmp_path: Any) -> None:
    """A runner that reports no-new-chapters doesn't open breaker."""
    circuit = MockCircuit()

    def up_to_date_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Return metadata but no output_filename (up-to-date)."""
        return MagicMock(
            returncode=0,
            stdout='{"output_filename": null}',
            stderr="",
        )

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=up_to_date_runner, circuit=circuit)

    # Run 10 times
    for _ in range(10):
        result = gateway.download("https://a.example/story", work_dir=tmp_path)
        assert result.ok is False

    # Breaker should still be closed
    assert circuit.is_open("host:a.example") is False


@pytest.mark.pins("EXP-269")
def test_a_successful_download_resets_the_count(tmp_path: Any) -> None:
    """Fail, fail, succeed, fail: breaker is CLOSED."""
    circuit = MockCircuit()
    call_sequence = ["fail", "fail", "success", "fail"]
    call_index = [0]

    def sequenced_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Return different outcomes in sequence."""
        outcome = call_sequence[call_index[0]]
        call_index[0] += 1

        if outcome == "fail":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        # Success: return valid metadata
        metadata = json.dumps(
            {
                "output_filename": "story.epub",
                "author": "Test",
                "category": "Test",
                "title": "Test",
            }
        )
        return MagicMock(
            returncode=0,
            stdout=metadata,
            stderr="",
        )

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=sequenced_runner, circuit=circuit)

    # First fail
    result = gateway.download("https://a.example/story", work_dir=tmp_path)
    assert result.ok is False

    # Second fail
    result = gateway.download("https://a.example/story", work_dir=tmp_path)
    assert result.ok is False

    # Success (count resets)
    result = gateway.download("https://a.example/story", work_dir=tmp_path)
    assert result.ok is True

    # Third fail (count is now 1, not 3)
    result = gateway.download("https://a.example/story", work_dir=tmp_path)
    assert result.ok is False

    # Breaker should still be closed
    assert circuit.is_open("host:a.example") is False


@pytest.mark.pins("EXP-269")
def test_the_metadata_and_download_paths_share_one_breaker(tmp_path: Any) -> None:
    """Two fetch_metadata timeouts + one download timeout opens it."""
    circuit = MockCircuit()
    call_count = [0]

    def failing_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Raise TimeoutExpired."""
        call_count[0] += 1
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=failing_runner, circuit=circuit)

    # Two metadata timeouts
    meta = gateway.fetch_metadata("https://a.example/story")
    assert meta is None

    meta = gateway.fetch_metadata("https://a.example/story")
    assert meta is None

    # One download timeout (third failure)
    result = gateway.download("https://a.example/story", work_dir=tmp_path)
    assert result.ok is False

    # Breaker should now be open
    assert circuit.is_open("host:a.example") is True

    # Fourth call should be refused
    meta = gateway.fetch_metadata("https://a.example/story")
    assert meta is None


@pytest.mark.pins("EXP-269")
def test_two_hosts_keep_two_breakers(tmp_path: Any) -> None:
    """Dead a.example doesn't stop b.example."""
    circuit = MockCircuit()
    call_counts: dict[str, int] = {"a.example": 0, "b.example": 0}

    def host_specific_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Fail for a.example, succeed for b.example."""
        for arg in argv:
            if "a.example" in arg:
                call_counts["a.example"] += 1
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            elif "b.example" in arg:
                call_counts["b.example"] += 1
                metadata = json.dumps(
                    {
                        "output_filename": "story.epub",
                        "author": "Test",
                        "category": "Test",
                        "title": "Test",
                    }
                )
                return MagicMock(
                    returncode=0,
                    stdout=metadata,
                    stderr="",
                )
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=host_specific_runner, circuit=circuit)

    # Three failures for a.example (opens breaker)
    for _ in range(3):
        result = gateway.download("https://a.example/story", work_dir=tmp_path)
        assert result.ok is False

    # a.example breaker is open
    assert circuit.is_open("host:a.example") is True

    # b.example should still work (3 successful calls)
    for _ in range(3):
        result = gateway.download("https://b.example/story", work_dir=tmp_path)
        assert result.ok is True

    # b.example breaker should still be closed
    assert circuit.is_open("host:b.example") is False


@pytest.mark.pins("EXP-269")
def test_the_gateway_is_unguarded_without_a_circuit(tmp_path: Any) -> None:
    """With circuit=None, runner is called even if it keeps timing out."""
    call_count = [0]

    def failing_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Raise TimeoutExpired."""
        call_count[0] += 1
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=failing_runner, circuit=None)

    # Call download 20 times
    for _ in range(20):
        result = gateway.download("https://a.example/story", work_dir=tmp_path)
        assert result.ok is False

    # The runner should have been called 20 times (no circuit protection)
    assert call_count[0] == 20


@pytest.mark.pins("EXP-269")
def test_the_skip_is_logged_at_info(tmp_path: Any, caplog: Any) -> None:
    """Skipped calls are logged at info level."""
    circuit = MockCircuit()
    circuit.open_keys.add("host:a.example")

    def failing_runner(
        argv: Any, cwd: Any, timeout: Any, cancel_event: Any = None, on_progress: Any = None
    ) -> Any:  # noqa: ARG001
        """Should not be called."""
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    config_file = tmp_path / "personal.ini"
    config_file.touch()

    gateway = FanFicFareCliGateway(config_file, runner=failing_runner, circuit=circuit)

    with caplog.at_level("INFO"):
        result = gateway.download("https://a.example/story", work_dir=tmp_path)
        assert result.ok is False

    assert "FanFicFare skipped: a.example is in a failure back-off" in caplog.text


@pytest.mark.pins("EXP-269")
def test_the_plugin_passes_its_context_circuit_to_the_gateway(tmp_path: Any) -> None:
    """Plugin passes ctx.circuit to the gateway."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin
    from fanficfare_source.pull import FanFicFarePull

    circuit = MockCircuit()
    config_file = tmp_path / "personal.ini"
    config_file.touch()

    # Create a gateway with a circuit
    gateway = FanFicFareCliGateway(config_file, circuit=circuit)
    assert gateway._circuit is circuit

    # Create a pull engine with that gateway
    pull_engine = FanFicFarePull(gateway)

    # Create the plugin
    plugin = FanFicFareSourcePlugin(pull=pull_engine)
    assert plugin is not None
