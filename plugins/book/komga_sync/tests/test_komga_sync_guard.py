"""Tests for the Komga sync's scan guard on a book without a file."""

from __future__ import annotations

from typing import Any

from ebookerr_sdk.testing import make_book_view
from komga_sync.service import KomgaService


class _ScanCountingKomga:
    """A Komga client double that finds nothing and counts library scans."""

    def __init__(self) -> None:
        self.scan_calls = 0
        self.connected = True

    def __getattr__(self, name: str) -> Any:
        """Answer every other client call with a harmless negative."""
        return lambda *args, **kwargs: None

    def test_connection(self) -> Any:
        """Report a reachable server."""
        from ebookerr_sdk.providers.connection import ConnectionTestResult

        return ConnectionTestResult("ok", "Connected.")

    def book_exists(self, _book_id: str) -> bool | None:
        """Report an unknown book."""
        return None

    def find_book_id(self, _title: str, _author: str | None) -> str | None:
        """Find nothing."""
        return None

    def trigger_library_scan(self) -> bool:
        """Count a library scan."""
        self.scan_calls += 1
        return True

    def list_library_books(self) -> list[tuple[str, str]]:
        """List no books."""
        return []

    def get_progression(self, _book_id: str) -> dict[str, Any]:
        """Return no progression."""
        return {}


def _service(client: _ScanCountingKomga, sleeps: list[float]) -> KomgaService:
    """Build a service that records its poll sleeps."""
    return KomgaService(
        client,  # type: ignore[arg-type]
        enabled=True,
        scan_retry_max=3,
        scan_retry_delay=0.0,
        sleep=sleeps.append,
        server_url="http://k/",
    )


def test_a_book_without_a_file_asks_for_no_scan_and_no_poll() -> None:
    """An unlinked book with no output file is not found without a scan or a poll."""
    client = _ScanCountingKomga()
    sleeps: list[float] = []

    result = _service(client, sleeps).sync(make_book_view(output_filename=None), allow_scan=True)

    assert client.scan_calls == 0
    assert sleeps == []
    assert result.ok is False
    assert result.not_found is True
    assert result.message == "the book has no file to find in Komga"


def test_a_book_with_a_file_still_scans() -> None:
    """The same unlinked book with a file asks for a scan and polls."""
    client = _ScanCountingKomga()
    sleeps: list[float] = []

    result = _service(client, sleeps).sync(make_book_view(), allow_scan=True)

    assert client.scan_calls == 1
    assert len(sleeps) == 3
    assert result.not_found is True
