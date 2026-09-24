"""The Komga REST client's shape — the seam the service depends on and the tests fake (moved from the core's gateway Protocols)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ebookerr_sdk.providers.connection import ConnectionTestResult


@runtime_checkable
class KomgaClient(Protocol):
    """Thin Komga REST client — calls only, no business rules.

    Business rules live in :mod:`komga_sync.service`. Contract
    documented in ARCHITECTURE.md §2.2.
    """

    def test_connection(self) -> ConnectionTestResult:
        """Probe reachability, the API key and any configured library id, in that order.

        Returns:
            A ``ConnectionTestResult`` whose outcome is ``"ok"`` on full success,
            ``"unreachable"`` when the server could not be contacted or answered with an
            unexpected error, ``"rejected"`` when it answered but refused the API key, or
            ``"misconfigured"`` when the key works but the configured library id does not
            resolve.
        """
        ...

    def find_book_id(self, title: str, author: str | None) -> str | None:
        """POST /api/v1/books/list matching *title* exactly, filtered by *author* client-side.

        Returns the single matching book's id, or None if there is no match,
        more than one match, or the request failed.
        """
        ...

    def get_book(self, komga_book_id: str) -> dict[str, Any] | None:
        """GET /api/v1/books/{id}; the book's metadata, or None if missing/failed."""
        ...

    def book_exists(self, komga_book_id: str) -> bool | None:
        """Whether the id still resolves: True (200), False (404), None (indeterminate)."""
        ...

    def get_series(self, series_id: str) -> dict[str, Any] | None:
        """GET /api/v1/series/{id}; the series' metadata, or None if missing/failed."""
        ...

    def trigger_analyze(self, komga_book_id: str) -> bool:
        """POST /api/v1/books/{id}/analyze; re-reads the file to refresh media metadata."""
        ...

    def trigger_library_scan(self) -> bool:
        """POST /api/v1/libraries/{library_id}/scan for the configured library.

        False if no library is configured or the request failed.
        """
        ...

    def patch_book_metadata(self, komga_book_id: str, patch: dict[str, Any]) -> bool:
        """PATCH /api/v1/books/{id}/metadata; merges *patch* into the book's metadata."""
        ...

    def patch_series_metadata(self, series_id: str, patch: dict[str, Any]) -> bool:
        """PATCH /api/v1/series/{id}/metadata; merges *patch* into the series' metadata."""
        ...

    def get_progression(self, komga_book_id: str) -> dict[str, Any]:
        """GET /api/v1/books/{id}/progression; the Readium progression body, or {} on failure."""
        ...

    def get_positions(self, komga_book_id: str) -> list[dict[str, Any]]:
        """GET /api/v1/books/{id}/positions; the reading-position list, or [] on any error."""
        ...

    def put_progression(self, komga_book_id: str, progression: dict[str, Any]) -> bool:
        """PUT /api/v1/books/{id}/progression; stores *progression* as the Readium state."""
        ...

    def list_library_books(self) -> list[tuple[str, str]]:
        """GET /api/v1/books for the configured library.

        Returns [(id, url)] for every book Komga has not marked ``deleted``, or [] when no
        library is configured or the request failed.
        """
        ...

    def delete_book_file(self, komga_book_id: str) -> bool:
        """DELETE /api/v1/books/{id}/file; Komga removes the EPUB from the shared volume."""
        ...

    def empty_trash(self) -> bool:
        """POST /api/v1/libraries/{library_id}/empty-trash; purges entries whose files are gone."""
        ...

    def scan_library(self, library_id: str) -> bool:
        """POST /api/v1/libraries/{library_id}/scan; triggers metadata rescan."""
        ...

    def empty_trash_for(self, library_id: str) -> bool:
        """POST /api/v1/libraries/{library_id}/empty-trash; purges entries whose files are gone."""
        ...
