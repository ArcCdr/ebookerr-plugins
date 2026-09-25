"""Real :class:`KomgaClient` over the Komga REST API (``X-API-Key`` auth).

Calls only — no business rules (those live in :mod:`komga_sync.service`).
Endpoints and payload shapes are grounded in the captured fixtures
(``tests/fixtures/README.md``); in particular EPUB read-progress is read/written
via the Readium ``/progression`` endpoint, never the page-based ``/read-progress``.
The HTTP session is injectable so tests mock it without live calls.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, cast

import requests
from ebookerr_sdk.domain.text_encoding import nfc
from ebookerr_sdk.download.availability import NO_ANSWER_ERRORS, is_unavailable_status
from ebookerr_sdk.providers.connection import (
    ConnectionTestResult,
    ProviderUnreachable,
    answered_result,
    unreachable_result,
)

logger = logging.getLogger(__name__)

_REDACTED = "***"
_BODY_CLIP = 200


def _clip(text: str | None) -> str:
    """One-line, 200-character-capped copy of a value, so a whole ERROR fits on one log line."""
    if not text:
        return "(empty)"
    flat = " ".join(text.split())
    return flat if len(flat) <= _BODY_CLIP else flat[:_BODY_CLIP] + "..."


def _redact(headers: Mapping[str, str | bytes]) -> dict[str, str | bytes]:
    """Copy headers with the X-API-Key masked, so DEBUG logs never leak the key."""
    return {
        key: (_REDACTED if key.lower() == "x-api-key" else value) for key, value in headers.items()
    }


class RequestsKomgaClient:
    """Thin REST wrapper around a :class:`requests.Session`."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        library_id: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Store connection settings and prepare the HTTP session.

        Args:
            base_url: Komga server root URL; a trailing slash is stripped.
            api_key: Sent as the ``X-API-Key`` header on every request.
            library_id: UUID of the Komga library scoped for library-wide
                operations (``trigger_library_scan``, ``empty_trash``,
                ``list_library_books``); pass ``""`` if none is configured.
            session: Injected :class:`requests.Session`, e.g. for tests; a
                new session is created when omitted.
            timeout: Per-request timeout, in seconds.
        """
        self._base_url = base_url.rstrip("/")
        self._library_id = library_id
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers["X-API-Key"] = api_key

    def _url(self, path: str) -> str:
        """Join ``path`` onto the configured base URL."""
        return f"{self._base_url}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Send an HTTP request and log the outcome at DEBUG.

        Args:
            method: HTTP verb (``"GET"``, ``"POST"``, ``"PATCH"``, ``"PUT"``,
                ``"DELETE"``).
            path: Request path, joined onto the configured base URL.
            **kwargs: Forwarded to :meth:`requests.Session.request` (e.g.
                ``params``, ``json``).

        Returns:
            The :class:`requests.Response`.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException`` (connection
                refused, timeout) or an answer of HTTP 502, 503 or 504 (``DFT-D41``).
        """
        try:
            response = self._session.request(
                method, self._url(path), timeout=self._timeout, **kwargs
            )
        except NO_ANSWER_ERRORS as exc:
            logger.debug("Komga %s %s -> no response", method, path)
            raise ProviderUnreachable(f"Komga is not reachable: {type(exc).__name__}") from exc
        logger.debug("Komga %s %s -> %s", method, path, response.status_code)
        if is_unavailable_status(response.status_code):
            raise ProviderUnreachable(f"Komga is not available: HTTP {response.status_code}")
        return response

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any] | None:
        """Parse ``response`` as JSON, or return ``None`` if the body isn't valid JSON."""
        try:
            return cast("dict[str, Any]", response.json())
        except ValueError:
            return None

    def test_connection(self) -> ConnectionTestResult:
        """Probe the server, the API key and the configured library, in that order.

        ``GET /actuator/info`` validates reachability and the key; when a ``library_id`` is
        configured, a second scoped ``GET /api/v1/libraries/<id>`` proves the id resolves, since
        that id scopes every later scan and book lookup.

        Returns:
            ``ConnectionTestResult`` whose outcome distinguishes an unreachable server, a rejected
            key, a library id that does not resolve, and full success. ``"unreachable"`` is
            returned for no answer or HTTP 502/503/504 (via ``unreachable_result``), and for any
            other unexpected status (via ``answered_result``); ``"rejected"`` for 401/403;
            ``"misconfigured"`` for a library id that does not resolve.
        """
        try:
            response = self._request("GET", "/actuator/info")
        except ProviderUnreachable:
            return unreachable_result("Komga", self._base_url)
        if response.status_code in (401, 403):
            return ConnectionTestResult("rejected", "Komga refused the API key.")
        if response.status_code != 200:
            return answered_result("Komga", response.status_code)
        if not self._library_id:
            return ConnectionTestResult("ok", "Connected.")

        try:
            lib_response = self._request("GET", f"/api/v1/libraries/{self._library_id}")
        except ProviderUnreachable:
            return unreachable_result("Komga", self._base_url)
        if lib_response.status_code == 404:
            return ConnectionTestResult(
                "misconfigured",
                f"No Komga library with id {self._library_id!r}. Check the Library id field.",
            )
        if lib_response.status_code in (401, 403):
            return ConnectionTestResult("rejected", "Komga refused the API key for that library.")
        if lib_response.status_code != 200:
            return answered_result("Komga", lib_response.status_code)
        return ConnectionTestResult("ok", "Connected.")

    def find_book_id(self, title: str, author: str | None) -> str | None:
        """Look up a book's Komga id by exact title (and optional author).

        Calls ``POST /api/v1/books/list`` with a title-equality condition,
        scoped to the configured library when one is set, then filters the
        returned page of matches by ``author`` client-side. The author
        comparison is done in Unicode Normalization Form C, so a decomposed
        name (macOS/SMB) matches its composed twin (``TXE-D3``).

        Args:
            title: Exact book title to match.
            author: Author name that must appear among the book's metadata
                authors, or ``None`` to skip author filtering.

        Returns:
            The single matching book's id, or ``None`` if there is no match,
            more than one match, or the request returned a non-2xx status.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        conds: list[dict[str, Any]] = [{"title": {"operator": "is", "value": title}}]
        if self._library_id:
            conds.append({"libraryId": {"operator": "is", "value": self._library_id}})
        body = {"condition": {"allOf": conds}}
        response = self._request("POST", "/api/v1/books/list", params={"size": 50}, json=body)
        if not response.ok:
            return None
        payload = self._json(response) or {}
        matches: list[str] = []
        for book in payload.get("content", []):
            names = [a.get("name") for a in book.get("metadata", {}).get("authors", [])]
            if author is None or nfc(author) in {nfc(n) for n in names if n}:
                matches.append(str(book["id"]))
        return matches[0] if len(matches) == 1 else None

    def get_book(self, komga_book_id: str) -> dict[str, Any] | None:
        """Fetch a book's Komga metadata.

        Args:
            komga_book_id: The book's Komga UUID.

        Returns:
            The parsed ``GET /api/v1/books/{id}`` JSON body, or ``None`` if
            the request returned a non-2xx status or the book does not exist.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        response = self._request("GET", f"/api/v1/books/{komga_book_id}")
        if not response.ok:
            return None
        return self._json(response)

    def book_exists(self, komga_book_id: str) -> bool | None:
        """Probe whether a Komga book id still resolves to a book.

        Komga re-issues book ids when it re-indexes a file, so a caller needs to tell
        "this id is gone" from "Komga did not answer" and fail closed on the latter.

        Args:
            komga_book_id: The book's Komga UUID.

        Returns:
            ``True`` when ``GET /api/v1/books/{id}`` returns HTTP 200; ``False`` on HTTP 404
            (the id no longer exists); ``None`` when the answer is indeterminate — any other
            status — so callers keep the current link.

        Raises:
            ProviderUnreachable: On any transport error or an HTTP 502/503/504 answer.
        """
        response = self._request("GET", f"/api/v1/books/{komga_book_id}")
        if response.status_code == 404:
            return False
        if response.ok:
            return True
        return None

    def get_series(self, series_id: str) -> dict[str, Any] | None:
        """Fetch a series' Komga metadata.

        Args:
            series_id: The series' Komga UUID.

        Returns:
            The parsed ``GET /api/v1/series/{id}`` JSON body, or ``None`` if
            the request returned a non-2xx status or the series does not exist.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        response = self._request("GET", f"/api/v1/series/{series_id}")
        if not response.ok:
            return None
        return self._json(response)

    def trigger_analyze(self, komga_book_id: str) -> bool:
        """Ask Komga to re-analyze a book (re-read the file to refresh media metadata).

        Args:
            komga_book_id: The book's Komga UUID.

        Returns:
            True iff ``POST /api/v1/books/{id}/analyze`` returned a 2xx response.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        response = self._request("POST", f"/api/v1/books/{komga_book_id}/analyze")
        return response.ok

    def trigger_library_scan(self) -> bool:
        """Trigger a scan of the configured library.

        Returns:
            True iff ``POST /api/v1/libraries/{id}/scan`` returned a 2xx
            response; False if no library is configured.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        if not self._library_id:
            return False
        response = self._request("POST", f"/api/v1/libraries/{self._library_id}/scan")
        return response.ok

    def scan_library(self, library_id: str) -> bool:
        """Trigger a scan of an arbitrary library.

        Args:
            library_id: The Komga library UUID to scan (unlike
                :meth:`trigger_library_scan`, not limited to the configured one).

        Returns:
            True iff ``POST /api/v1/libraries/{id}/scan`` returned a 2xx response.
        """
        return self._mutate("POST", f"/api/v1/libraries/{library_id}/scan")

    def delete_book_file(self, komga_book_id: str) -> bool:
        """Delete a book's underlying file; Komga removes the EPUB from the shared volume.

        Args:
            komga_book_id: The book's Komga UUID.

        Returns:
            True iff ``DELETE /api/v1/books/{id}/file`` returned a 2xx response.
        """
        return self._mutate("DELETE", f"/api/v1/books/{komga_book_id}/file")

    def empty_trash(self) -> bool:
        """Empty the trash of the configured library (purges entries whose files are gone).

        Returns:
            True iff the request returned a 2xx response; False if no library
            is configured or the request failed.
        """
        if not self._library_id:
            return False
        return self._mutate("POST", f"/api/v1/libraries/{self._library_id}/empty-trash")

    def empty_trash_for(self, library_id: str) -> bool:
        """Empty the trash of an arbitrary library (purges entries whose files are gone).

        Args:
            library_id: The Komga library UUID to empty (unlike
                :meth:`empty_trash`, not limited to the configured one).

        Returns:
            True iff ``POST /api/v1/libraries/{id}/empty-trash`` returned a
            2xx response.
        """
        return self._mutate("POST", f"/api/v1/libraries/{library_id}/empty-trash")

    def patch_book_metadata(self, komga_book_id: str, patch: dict[str, Any]) -> bool:
        """Partially update a book's metadata.

        Args:
            komga_book_id: The book's Komga UUID.
            patch: Fields to merge into the book's metadata (Komga's
                ``BookMetadataUpdateDto`` shape).

        Returns:
            True iff ``PATCH /api/v1/books/{id}/metadata`` returned a 2xx response.
        """
        return self._mutate("PATCH", f"/api/v1/books/{komga_book_id}/metadata", patch)

    def patch_series_metadata(self, series_id: str, patch: dict[str, Any]) -> bool:
        """Partially update a series' metadata.

        Args:
            series_id: The series' Komga UUID.
            patch: Fields to merge into the series' metadata (Komga's
                ``SeriesMetadataUpdateDto`` shape).

        Returns:
            True iff ``PATCH /api/v1/series/{id}/metadata`` returned a 2xx response.
        """
        return self._mutate("PATCH", f"/api/v1/series/{series_id}/metadata", patch)

    def _mutate(self, method: str, path: str, payload: dict[str, Any] | None = None) -> bool:
        """Send a mutation; log the server's explanation and payload on any non-2xx, return False.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        kwargs: dict[str, Any] = {}
        if payload is not None:
            kwargs["json"] = payload
        response = self._request(method, path, **kwargs)
        if not response.ok:
            logger.debug(
                "Komga %s %s -> HTTP %s headers=%s payload=%s body=%s",
                method,
                path,
                response.status_code,
                _redact(self._session.headers),
                payload,
                response.text,
            )
            logger.error(
                "Komga %s %s returned HTTP %s: %s | payload=%s",
                method,
                path,
                response.status_code,
                _clip(response.text),
                _clip(json.dumps(payload, default=str)) if payload is not None else "-",
            )
            return False
        return True

    def get_progression(self, komga_book_id: str) -> dict[str, Any]:
        """Fetch a book's Readium reading-progression state.

        Args:
            komga_book_id: The book's Komga UUID.

        Returns:
            The parsed ``GET /api/v1/books/{id}/progression`` JSON body
            (a Readium ``Progression``), or ``{}`` if the request returned a non-2xx status.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        response = self._request("GET", f"/api/v1/books/{komga_book_id}/progression")
        if not response.ok:
            return {}
        return self._json(response) or {}

    def get_positions(self, komga_book_id: str) -> list[dict[str, Any]]:
        """R2 positions list for a book (GET /api/v1/books/{id}/positions); [] on HTTP error.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        response = self._request("GET", f"/api/v1/books/{komga_book_id}/positions")
        if not response.ok:
            return []
        payload = self._json(response) or {}
        positions = payload.get("positions")
        return positions if isinstance(positions, list) else []

    def put_progression(self, komga_book_id: str, progression: dict[str, Any]) -> bool:
        """Write a book's Readium reading-progression state.

        Args:
            komga_book_id: The book's Komga UUID.
            progression: The Readium ``Progression`` body to store.

        Returns:
            True iff ``PUT /api/v1/books/{id}/progression`` returned a 2xx response.
        """
        return self._mutate("PUT", f"/api/v1/books/{komga_book_id}/progression", progression)

    def list_library_books(self) -> list[tuple[str, str]]:
        """Return ``[(id, url)]`` for all books in the configured library (T7).

        Books Komga has marked ``deleted`` (file gone, entry pending trash) are excluded: a
        trashed entry must never win a path match.

        Used for client-side path matching in :meth:`KomgaService.enrich`.
        Returns ``[]`` when no library is configured or on any HTTP error.

        Raises:
            ProviderUnreachable: On any transport error.
        """
        if not self._library_id:
            return []
        response = self._request(
            "GET",
            "/api/v1/books",
            params={"library_id": self._library_id, "unpaged": "true"},
        )
        if not response.ok:
            return []
        payload = self._json(response) or {}
        books = [
            book
            for book in payload.get("content", [])
            if "id" in book and "url" in book and not book.get("deleted")
        ]
        skipped = len(payload.get("content", [])) - len(books)
        if skipped:
            logger.debug("Komga library listing skipped %d book(s) marked deleted", skipped)
        return [(str(book["id"]), str(book["url"])) for book in books]
