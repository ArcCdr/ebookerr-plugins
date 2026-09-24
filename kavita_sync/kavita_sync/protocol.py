"""The Kavita REST client's shape — the seam the service depends on and the tests fake (moved from the core's gateway Protocols)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ebookerr_sdk.providers.connection import ConnectionTestResult

if TYPE_CHECKING:
    from kavita_sync.client import KavitaRef, KavitaSeriesUnresolved


@runtime_checkable
class KavitaClient(Protocol):
    """Thin Kavita REST client — calls only, no business rules.

    Business rules live in :mod:`kavita_sync.service`. Contract
    documented in ARCHITECTURE.md §2.2.
    """

    def test_connection(self) -> ConnectionTestResult:
        """Probe reachability and whether a plugin auth token can be obtained, in that order.

        Returns:
            A ``ConnectionTestResult`` whose outcome is ``"ok"`` on full success,
            ``"unreachable"`` when the server could not be contacted or answered with an
            unexpected error, ``"rejected"`` when it answered but refused the credentials, or
            ``"misconfigured"`` when the credentials work but a configured id does not resolve.
        """
        ...

    def find_chapter(self, output_filename: str) -> KavitaRef | KavitaSeriesUnresolved | None:
        """Search Kavita by *output_filename*.

        A chapter file matches when its path ends with *output_filename*, compared
        case-insensitively; a file with the same basename under a different folder
        does not match.

        Returns:
            The matching chapter/volume/series; ``None`` when no chapter file matches;
            ``KavitaSeriesUnresolved`` when the file matched but its series did not.
        """
        ...

    def get_progress(self, chapter_id: int) -> dict[str, Any]:
        """Reading progress for *chapter_id*, or {} on error."""
        ...

    def save_progress(self, ref: KavitaRef, page_num: int) -> bool:
        """Save *page_num* as the reading progress for the chapter in *ref*."""
        ...

    def rate_series(self, series_id: int, rating: float) -> bool:
        """Set the current user's rating for *series_id*."""
        ...

    def series_rating(self, series_id: int) -> float | None:
        """The current user's rating (1-5) for *series_id*, or None when unrated/failed."""
        ...

    def scan_folder(self, folder_path: str) -> bool:
        """Ask Kavita to scan *folder_path* for new content."""
        ...

    def scan_library(self, library_id: int) -> bool:
        """Ask Kavita to rescan *library_id* for changes."""
        ...

    def book_chapters(self, chapter_id: int) -> list[dict[str, Any]]:
        """The book's table of contents for *chapter_id*, or [] on error."""
        ...
