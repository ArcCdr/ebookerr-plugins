"""Real :class:`KavitaClient` over the Kavita REST API (plugin token auth).

Calls only — no business rules (those live in :mod:`src.services.kavita_service`).
The HTTP session is injectable so tests mock it without live calls. Transport failures and
HTTP 502/503/504 answers raise ``ProviderUnreachable`` (``EXP-192``, ``DFT-D41``);
``test_connection`` is the probe and reports instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
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

_BODY_CLIP = 200


def _clip(text: str | None) -> str:
    """One-line, 200-character-capped copy of a value, so a whole ERROR fits on one log line."""
    if not text:
        return "(empty)"
    flat = " ".join(text.split())
    return flat if len(flat) <= _BODY_CLIP else flat[:_BODY_CLIP] + "..."


def _log_http_error(
    operation: str,
    method: str,
    path: str,
    obj: str,
    response: requests.Response,
    payload: dict[str, Any] | None,
) -> None:
    """Log one ERROR naming the call, the object, the server's own explanation and what was sent.

    Never receives headers or query parameters, so neither the plugin API key nor the bearer
    token can reach the log.
    """
    logger.error(
        "Kavita %s failed (HTTP %s) for %s (%s %s): %s | payload=%s",
        operation,
        response.status_code,
        obj,
        method,
        path,
        _clip(response.text),
        _clip(json.dumps(payload, default=str)) if payload is not None else "-",
    )


@dataclass(frozen=True)
class KavitaRef:
    """Reference to a Kavita chapter."""

    chapter_id: int
    volume_id: int
    series_id: int
    library_id: int
    total_pages: int


@dataclass(frozen=True)
class KavitaSeriesUnresolved:
    """A file Kavita has indexed whose series could not be resolved (``EXP-190``).

    Distinct from ``None`` ("no chapter file matches"): the book *is* in Kavita, so the
    caller must never report it as not found.

    Attributes:
        stem: The searched filename (basename with extension).
        detail: Why the series could not be resolved, for the outcome message.
    """

    stem: str
    detail: str

    @property
    def message(self) -> str:
        """User-facing outcome naming the file and the cause."""
        return (
            f'"{self.stem}" is indexed in Kavita but its series could not be resolved: '
            f"{self.detail}"
        )


class RequestsKavitaClient:
    """Thin REST wrapper around a :class:`requests.Session`."""

    def __init__(
        self,
        server: str,
        api_key: str,
        *,
        session: requests.Session | None = None,
        external_url: str | None = None,
    ) -> None:
        """Store connection settings; the auth token is fetched lazily on first use.

        Args:
            server: Kavita server root URL; a trailing slash is stripped.
            api_key: Kavita plugin API key, exchanged for a bearer token by
                :meth:`_token`.
            session: Injected :class:`requests.Session`, e.g. for tests; a
                new session is created when omitted.
            external_url: Kept on the instance but not otherwise used here.
        """
        self._server = server.rstrip("/")
        self._api_key = api_key
        self._external_url = external_url
        self._session = session or requests.Session()
        self._token_cache: str | None | bool = None  # None=failed, bool=pending, str=cached

    def _send(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Send one request; transport failures and HTTP 502/503/504 raise ``ProviderUnreachable``.

        Args:
            method: HTTP verb.
            path: Path under the server root (leading slash).
            **kwargs: Forwarded to ``requests.Session.request`` (``params``, ``json``, ``headers``).

        Returns:
            The response of any answer other than HTTP 502/503/504.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException`` or an answer of HTTP 502,
                503 or 504 (``DFT-D41``).
        """
        try:
            response = self._session.request(
                method, f"{self._server}{path}", timeout=30.0, **kwargs
            )
        except NO_ANSWER_ERRORS as exc:
            logger.debug("Kavita %s %s -> no response (%s)", method, path, type(exc).__name__)
            raise ProviderUnreachable(f"Kavita is not reachable: {type(exc).__name__}") from exc
        if is_unavailable_status(response.status_code):
            logger.debug("Kavita %s %s -> HTTP %s", method, path, response.status_code)
            raise ProviderUnreachable(f"Kavita is not available: HTTP {response.status_code}")
        return response

    def _token(self) -> str | None:
        """Authenticate with Kavita; raise on transport failure or return None on error.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException`` or a 502/503/504 answer —
                which is not cached, so the next call authenticates again.
        """
        if self._token_cache is not None:
            # Cache miss (False) or cache hit (str token)
            return self._token_cache if isinstance(self._token_cache, str) else None

        try:
            response = self._send(
                "POST",
                "/api/Plugin/authenticate",
                params={"apiKey": self._api_key, "pluginName": "ebookerr"},
            )
            if response.status_code != 200:
                self._token_cache = False
                return None

            data = cast("dict[str, Any]", response.json())
            token = data.get("token")
            if token:
                token = cast(str, token)
                self._token_cache = token
                return token
            else:
                self._token_cache = False
                return None
        except ValueError:
            # Invalid JSON
            self._token_cache = False
            return None

    def _headers(self) -> dict[str, str]:
        """Return auth headers with cached token, or empty dict if no token."""
        token = self._token()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def test_connection(self) -> ConnectionTestResult:
        """Probe reachability and token authentication (``DFT-D50``).

        Returns:
            ``"ok"`` on success; ``"rejected"`` when the server refused the key or no token;
            ``"unreachable"`` with "Could not reach the Kavita server…" when it did not answer,
            answered 502/503/504, or answered non-JSON; ``Kavita answered HTTP <n>.`` for
            any other status.
        """
        try:
            response = self._send(
                "POST",
                "/api/Plugin/authenticate",
                params={"apiKey": self._api_key, "pluginName": "ebookerr"},
            )
        except ProviderUnreachable:
            return unreachable_result("Kavita", self._server)
        if response.status_code in (401, 403):
            return ConnectionTestResult("rejected", "Kavita refused the API key.")
        if response.status_code != 200:
            return answered_result("Kavita", response.status_code)
        try:
            data = cast("dict[str, Any]", response.json())
        except ValueError:
            return unreachable_result("Kavita", self._server)
        if data.get("token"):
            return ConnectionTestResult("ok", "Connected.")
        return ConnectionTestResult("rejected", "Kavita refused the API key.")

    def _find_matching_file(
        self, output_filename: str, chapters: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Scan chapters for a file whose path ends with the book's library-relative path.

        The comparison is case-insensitive and on the whole relative path, not the basename:
        two library rows may legitimately share a basename under different author folders,
        and matching on the basename alone links both to one chapter (``EXP-149``). Both the
        stored path and the server's path are compared in Unicode Normalization Form C, so a
        decomposed name (macOS/SMB) matches its composed twin (``TXE-D3``).

        Args:
            output_filename: The book's library-relative path, ``"Author/Title.epub"``.
            chapters: The ``chapters`` array from Kavita's search answer.

        Returns:
            The matching ``(chapter, file)`` pair, or ``None`` when no file matches.
        """
        suffix = "/" + nfc(output_filename.replace("\\", "/")).casefold().lstrip("/")
        basename = nfc(Path(output_filename).name).casefold()
        near_miss: str | None = None
        for chapter in chapters:
            for file in chapter.get("files", []):
                file_path = str(file.get("filePath", ""))
                normalised = "/" + nfc(file_path.replace("\\", "/")).casefold().lstrip("/")
                if (
                    normalised.endswith(suffix)
                    and chapter.get("id")
                    and chapter.get("volumeId")
                    and file.get("id")
                ):
                    return chapter, file
                if nfc(Path(file_path).name).casefold() == basename:
                    near_miss = file_path
        if near_miss is not None:
            logger.debug(
                "Kavita: %r has the right basename but a different path (%s)",
                output_filename,
                near_miss,
            )
        return None

    def _get_series_ids(
        self,
        file_id: int,
        headers: dict[str, str],
        fallback_series: list[dict[str, Any]],
        query: str,
        stem: str,
    ) -> tuple[int, int] | KavitaSeriesUnresolved:
        """Get series and library IDs from the file or fallback to search results.

        Raises on a transport failure; returns ``KavitaSeriesUnresolved`` if the file was
        matched but its series could not be resolved. The name fallback compares both sides
        in Unicode Normalization Form C, so a decomposed series name matches its composed
        query twin (``TXE-D3``).

        Args:
            file_id: The Kavita file ID.
            headers: HTTP headers with auth token.
            fallback_series: Search results from the series array.
            query: The EPUB filename stem (basename without extension).
            stem: The searched filename (basename with extension).

        Returns:
            Tuple of (series_id, library_id) on success, or ``KavitaSeriesUnresolved`` when
            the file was matched but its series could not be resolved.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        detail: str | None = None

        try:
            response = self._send(
                "GET",
                "/api/Search/series-for-mangafile",
                params={"mangaFileId": file_id},
                headers=headers,
            )
            if response.status_code == 200:
                series_data = cast("dict[str, Any]", response.json())
                series_id = series_data.get("id")
                library_id = series_data.get("libraryId")
                if series_id and library_id:
                    logger.debug(
                        "Kavita series resolved by file for %s: series=%s, library=%s",
                        stem,
                        series_id,
                        library_id,
                    )
                    return int(series_id), int(library_id)
                detail = "series-for-mangafile answered without an id"
            else:
                detail = f"series-for-mangafile answered HTTP {response.status_code}"
        except ValueError:
            detail = "series-for-mangafile could not be read"

        # Fallback only after the per-file lookup failed
        matches = [
            s
            for s in fallback_series
            if nfc(str(s.get("name") or "")).casefold() == nfc(query).casefold()
            and s.get("seriesId")
            and s.get("libraryId")
        ]

        if len(matches) == 1:
            m = matches[0]
            logger.debug(
                "Kavita series resolved by name for %s: series=%s (%s)",
                stem,
                m["seriesId"],
                detail,
            )
            return int(m["seriesId"]), int(m["libraryId"])

        if len(matches) == 0:
            return KavitaSeriesUnresolved(stem, f"{detail}; no series named {query!r}")

        # Multiple matches
        logger.warning(
            "Kavita series lookup ambiguous for %s: %d series named %r — leaving the book unlinked",
            stem,
            len(matches),
            query,
        )
        return KavitaSeriesUnresolved(stem, f"{detail}; {len(matches)} series named {query!r}")

    def find_chapter(self, output_filename: str) -> KavitaRef | KavitaSeriesUnresolved | None:
        """Find a chapter by output filename.

        The match is on the book's library-relative path (case-insensitively), not on the
        basename. On a non-2xx answer or bad JSON, returns None; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            The matching chapter/volume/series; ``None`` when no chapter file matches;
            ``KavitaSeriesUnresolved`` when the file matched but its series did not.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        stem = Path(output_filename).name  # basename with extension
        query = Path(output_filename).stem  # basename without extension

        headers = self._headers()
        response = self._send(
            "GET",
            "/api/Search/search",
            params={
                "queryString": query,
                "includeChapterAndFiles": "true",
            },
            headers=headers,
        )

        if response.status_code != 200:
            return None

        try:
            data = cast("dict[str, Any]", response.json())
        except ValueError:
            return None

        match = self._find_matching_file(output_filename, data.get("chapters", []))
        if not match:
            logger.debug("Kavita: no chapter matches %s", output_filename)
            return None

        chapter, file = match
        file_id = cast(int, file.get("id"))
        series_ids = self._get_series_ids(file_id, headers, data.get("series", []), query, stem)
        if isinstance(series_ids, KavitaSeriesUnresolved):
            return series_ids

        series_id, library_id = series_ids
        ref = KavitaRef(
            chapter_id=cast(int, chapter.get("id")),
            volume_id=cast(int, chapter.get("volumeId")),
            series_id=series_id,
            library_id=library_id,
            total_pages=cast(int, chapter.get("pages", 0)),
        )
        logger.debug(
            "Kavita chapter matched for %s (series=%s, chapter=%s)",
            output_filename,
            ref.series_id,
            ref.chapter_id,
        )
        return ref

    def get_progress(self, chapter_id: int) -> dict[str, Any]:
        """Get reading progress for a chapter.

        On a non-2xx answer or bad JSON, returns {}; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            The ProgressDto, or {} on a non-2xx answer or bad JSON.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        headers = self._headers()
        response = self._send(
            "GET",
            "/api/Reader/get-progress",
            params={"chapterId": chapter_id},
            headers=headers,
        )
        if response.status_code == 200:
            try:
                data = cast("dict[str, Any]", response.json())
                return data
            except ValueError:
                return {}
        return {}

    def save_progress(self, ref: KavitaRef, page_num: int) -> bool:
        """Save reading progress for a chapter.

        On a non-2xx answer, returns False; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            True on a 2xx answer, False on a non-2xx answer.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        headers = self._headers()
        body = {
            "volumeId": ref.volume_id,
            "chapterId": ref.chapter_id,
            "pageNum": page_num,
            "seriesId": ref.series_id,
            "libraryId": ref.library_id,
        }
        response = self._send(
            "POST",
            "/api/Reader/progress",
            json=body,
            headers=headers,
        )
        if response.status_code >= 200 and response.status_code < 300:
            return True
        _log_http_error(
            "save_progress",
            "POST",
            "/api/Reader/progress",
            f"chapter {ref.chapter_id}",
            response,
            body,
        )
        return False

    def rate_series(self, series_id: int, rating: float) -> bool:
        """Rate a series.

        On a non-2xx answer, returns False; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            True on a 2xx answer, False on a non-2xx answer.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        headers = self._headers()
        body = {"seriesId": series_id, "userRating": rating}
        response = self._send(
            "POST",
            "/api/Rating/series",
            json=body,
            headers=headers,
        )
        if response.status_code >= 200 and response.status_code < 300:
            return True
        _log_http_error(
            "rate_series",
            "POST",
            "/api/Rating/series",
            f"series {series_id}",
            response,
            body,
        )
        return False

    def series_rating(self, series_id: int) -> float | None:
        """Get the current user's rating for a series (1–5), or None when unrated.

        Kavita stores the logged-in user's rating on the series entity itself
        (``GET /api/Series/{id}`` → ``userRating``); the ``Rating/overall-series``
        endpoint returns the community/external ``averageScore``, not the user's
        rating, so it must not be used here. A ``userRating`` of 0 means unrated.
        On a non-2xx answer or bad JSON, returns None; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            The user's rating (1-5), or None when unrated, missing, or on a non-2xx answer.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        headers = self._headers()
        response = self._send(
            "GET",
            f"/api/Series/{series_id}",
            headers=headers,
        )
        if response.status_code == 200:
            try:
                data = cast("dict[str, Any]", response.json())
                rating = data.get("userRating")
                if rating:  # non-zero, non-None
                    return cast(float, rating)
            except ValueError:
                pass
        return None

    def scan_folder(self, folder_path: str) -> bool:
        """Scan a folder for new content.

        On a non-2xx answer, returns False; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            True on a 2xx answer, False on a non-2xx answer.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        logger.debug("Kavita folder scan requested: %s", folder_path)
        headers = self._headers()
        body = {
            "apiKey": self._api_key,
            "folderPath": folder_path,
            "abortOnNoSeriesMatch": False,
        }
        response = self._send(
            "POST",
            "/api/Library/scan-folder",
            json=body,
            headers=headers,
        )
        return response.status_code >= 200 and response.status_code < 300

    def scan_library(self, library_id: int) -> bool:
        """Rescan a library for changes.

        On a non-2xx answer, returns False; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            True on a 2xx answer, False on a non-2xx answer.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        headers = self._headers()
        response = self._send(
            "POST",
            "/api/Library/scan",
            params={"libraryId": library_id, "force": "false"},
            headers=headers,
        )
        if response.status_code >= 200 and response.status_code < 300:
            return True
        logger.warning(
            "Kavita library scan request for library %s answered HTTP %s",
            library_id,
            response.status_code,
        )
        return False

    def book_chapters(self, chapter_id: int) -> list[dict[str, Any]]:
        """Book TOC (GET /api/Book/{chapterId}/chapters) -> BookChapterItem list.

        On a non-2xx answer or bad JSON, returns []; a transport failure raises
        ``ProviderUnreachable``.

        Returns:
            The BookChapterItem list, or [] on a non-2xx answer or bad JSON.

        Raises:
            ProviderUnreachable: On any ``requests.RequestException``.
        """
        headers = self._headers()
        response = self._send(
            "GET",
            f"/api/Book/{chapter_id}/chapters",
            headers=headers,
        )
        if response.status_code == 200:
            try:
                data = response.json()
                return data if isinstance(data, list) else []
            except ValueError:
                return []
        return []
