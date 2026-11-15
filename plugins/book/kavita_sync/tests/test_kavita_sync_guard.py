"""Tests for the Kavita sync's behaviour on a book without a file."""

from __future__ import annotations

from typing import Any

from ebookerr_sdk.providers.connection import ConnectionTestResult
from ebookerr_sdk.testing import make_book_view
from kavita_sync.service import KavitaService


class _ScanCountingKavita:
    """A Kavita client double that finds nothing and records every scan."""

    def __init__(self) -> None:
        self.scan_folder_calls: list[str] = []
        self.scan_library_calls: list[int] = []

    def __getattr__(self, name: str) -> Any:
        """Answer every other client call with a harmless negative."""
        return lambda *args, **kwargs: None

    def test_connection(self) -> ConnectionTestResult:
        """Report a reachable server."""
        return ConnectionTestResult("ok", "Connected.")

    def find_chapter(self, _output_filename: str) -> None:
        """Find nothing."""
        return None

    def scan_folder(self, folder_path: str) -> bool:
        """Record a folder scan."""
        self.scan_folder_calls.append(folder_path)
        return True

    def scan_library(self, library_id: int) -> bool:
        """Record a library scan."""
        self.scan_library_calls.append(library_id)
        return True


def test_a_book_without_a_file_asks_for_no_scan_and_is_not_found() -> None:
    """An unlinked book with no output file triggers no scan and is not found."""
    client = _ScanCountingKavita()

    result = KavitaService(client, enabled=True, sleep=lambda _s: None).sync(  # type: ignore[arg-type]
        make_book_view(output_filename=None)
    )

    assert client.scan_folder_calls == []
    assert client.scan_library_calls == []
    assert result.ok is False
    assert result.not_found is True
