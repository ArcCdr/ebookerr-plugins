"""Tests for the Komga REST client (HTTP mocked with `responses`; no live calls)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import requests
import responses
from ebookerr_sdk.providers.connection import ProviderUnreachable

from komga_sync.client import RequestsKomgaClient
from komga_sync.protocol import KomgaClient

BASE = "http://komga.test"
LIBRARY = "0N0N8DZSTYAHT"
_KOMGA_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _client(library: str = LIBRARY) -> RequestsKomgaClient:
    return RequestsKomgaClient(BASE, "secret-key", library)


def _fixture(name: str) -> dict:
    return json.loads((_KOMGA_FIXTURES / name).read_text())


@responses.activate
def test_connection_true_on_200() -> None:
    """200 on /actuator/info with no library configured maps to ok."""
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    result = _client(library="").test_connection()
    assert result.outcome == "ok"
    assert result.ok is True
    assert result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_false_on_non_200() -> None:
    """A non-401/403 error status maps to unreachable, naming the status code."""
    responses.add(responses.GET, f"{BASE}/actuator/info", status=500)
    result = _client(library="").test_connection()
    assert result.outcome == "unreachable"
    assert result.ok is False
    assert "500" in result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_false_on_network_error() -> None:
    """No response at all (connection refused, timeout, ...) maps to unreachable."""
    responses.add(responses.GET, f"{BASE}/actuator/info", body=requests.ConnectionError())
    result = _client(library="").test_connection()
    assert result.outcome == "unreachable"
    assert result.ok is False
    assert result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_rejected_on_401() -> None:
    """A 401 on /actuator/info maps to rejected — a bad API key, not an outage."""
    responses.add(responses.GET, f"{BASE}/actuator/info", status=401)
    result = _client(library="").test_connection()
    assert result.outcome == "rejected"
    assert result.ok is False
    assert result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_rejected_on_403() -> None:
    """A 403 on /actuator/info also maps to rejected."""
    responses.add(responses.GET, f"{BASE}/actuator/info", status=403)
    result = _client(library="").test_connection()
    assert result.outcome == "rejected"
    assert result.ok is False
    assert result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_ok_when_library_resolves() -> None:
    """A 200 on the configured library id, after a 200 on /actuator/info, maps to ok."""
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    responses.add(
        responses.GET, f"{BASE}/api/v1/libraries/{LIBRARY}", json={"id": LIBRARY}, status=200
    )
    result = _client().test_connection()
    assert result.outcome == "ok"
    assert result.ok is True
    assert result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_misconfigured_on_unknown_library() -> None:
    """A 404 on the configured library id maps to misconfigured and names the id."""
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    responses.add(responses.GET, f"{BASE}/api/v1/libraries/{LIBRARY}", status=404)
    result = _client().test_connection()
    assert result.outcome == "misconfigured"
    assert result.ok is False
    assert LIBRARY in result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_rejected_on_library_credentials() -> None:
    """A 401 on the library probe maps to rejected, with a library-specific message."""
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    responses.add(responses.GET, f"{BASE}/api/v1/libraries/{LIBRARY}", status=401)
    result = _client().test_connection()
    assert result.outcome == "rejected"
    assert result.ok is False
    assert result.message
    assert "secret-key" not in result.message


@responses.activate
def test_connection_unreachable_when_library_probe_has_no_response() -> None:
    """A network error on the library probe maps to unreachable."""
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    responses.add(
        responses.GET, f"{BASE}/api/v1/libraries/{LIBRARY}", body=requests.ConnectionError()
    )
    result = _client().test_connection()
    assert result.outcome == "unreachable"
    assert result.ok is False
    assert result.message
    assert "secret-key" not in result.message


@responses.activate
def test_requests_send_api_key_header() -> None:
    responses.add(responses.GET, f"{BASE}/actuator/info", json={}, status=200)
    _client(library="").test_connection()
    assert responses.calls[0].request.headers["X-API-Key"] == "secret-key"


@responses.activate
def test_find_book_id_unique_match() -> None:
    responses.add(
        responses.POST,
        f"{BASE}/api/v1/books/list",
        json=_fixture("books_list_search.json"),
        status=200,
    )
    assert _client().find_book_id("The Lottery Winner - Pt. 01", "JQueen9") == "0QJH5QMC8ES03"


@responses.activate
def test_find_book_id_matches_a_decomposed_author() -> None:
    """A server author in decomposed Unicode still matches a composed query author (TXE-D3)."""
    responses.add(
        responses.POST,
        f"{BASE}/api/v1/books/list",
        json={
            "content": [
                {
                    "id": "0QJH5QMC8ES03",
                    "metadata": {"authors": [{"name": "Zoë", "role": "writer"}]},
                }
            ],
            "totalElements": 1,
        },
        status=200,
    )
    assert _client().find_book_id("The Lottery Winner - Pt. 01", "Zoë") == "0QJH5QMC8ES03"


@responses.activate
def test_find_book_id_author_mismatch() -> None:
    responses.add(
        responses.POST,
        f"{BASE}/api/v1/books/list",
        json=_fixture("books_list_search.json"),
        status=200,
    )
    assert _client().find_book_id("The Lottery Winner - Pt. 01", "Someone Else") is None


@responses.activate
def test_find_book_id_sends_library_id_condition() -> None:
    responses.add(
        responses.POST,
        f"{BASE}/api/v1/books/list",
        json=_fixture("books_list_search.json"),
        status=200,
    )
    _client().find_book_id("The Lottery Winner - Pt. 01", "JQueen9")
    body = json.loads(responses.calls[0].request.body)
    assert {"libraryId": {"operator": "is", "value": LIBRARY}} in body["condition"]["allOf"]


@responses.activate
def test_find_book_id_ignores_other_libraries() -> None:
    # Real Komga returns empty when libraryId condition doesn't match — server-side filter.
    responses.add(
        responses.POST,
        f"{BASE}/api/v1/books/list",
        json={"content": [], "totalElements": 0},
        status=200,
    )
    result = _client("OTHER").find_book_id("The Lottery Winner - Pt. 01", "JQueen9")
    assert result is None
    body = json.loads(responses.calls[0].request.body)
    assert {"libraryId": {"operator": "is", "value": "OTHER"}} in body["condition"]["allOf"]


@responses.activate
def test_find_book_id_no_library_id_omits_library_condition() -> None:
    responses.add(
        responses.POST,
        f"{BASE}/api/v1/books/list",
        json=_fixture("books_list_search.json"),
        status=200,
    )
    _client("").find_book_id("The Lottery Winner - Pt. 01", "JQueen9")
    body = json.loads(responses.calls[0].request.body)
    assert not any("libraryId" in c for c in body["condition"]["allOf"])


@responses.activate
def test_find_book_id_no_results() -> None:
    responses.add(
        responses.POST,
        f"{BASE}/api/v1/books/list",
        json={"content": [], "totalElements": 0},
        status=200,
    )
    assert _client().find_book_id("Nothing", None) is None


@responses.activate
def test_find_book_id_server_error() -> None:
    responses.add(responses.POST, f"{BASE}/api/v1/books/list", status=500)
    assert _client().find_book_id("x", None) is None


@responses.activate
def test_get_book() -> None:
    book = _fixture("book_0QJH5QMC8ES03.json")
    responses.add(responses.GET, f"{BASE}/api/v1/books/0QJH5QMC8ES03", json=book, status=200)
    assert _client().get_book("0QJH5QMC8ES03")["seriesId"] == "0QJH5QMC8ES02"


@responses.activate
def test_get_book_missing() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/X", status=404)
    assert _client().get_book("X") is None


@responses.activate
def test_get_book_invalid_json_returns_none() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/B", body="<<not json>>", status=200)
    assert _client().get_book("B") is None


@responses.activate
def test_book_exists_true_on_200() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/KB1", json={"id": "KB1"}, status=200)
    assert _client().book_exists("KB1") is True


@responses.activate
def test_book_exists_false_on_404() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/GONE", status=404)
    assert _client().book_exists("GONE") is False


@responses.activate
def test_book_exists_none_on_server_error() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/KB1", status=500)
    assert _client().book_exists("KB1") is None


@responses.activate
def test_book_exists_raises_provider_unreachable_on_a_network_error() -> None:
    """book_exists raises ProviderUnreachable when the request fails to send."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    responses.add(responses.GET, f"{BASE}/api/v1/books/KB1", body=requests.ConnectionError())
    with pytest.raises(ProviderUnreachable):
        _client().book_exists("KB1")


@responses.activate
def test_book_exists_true_on_200_with_unparseable_body() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/KB1", body="<<not json>>", status=200)
    assert _client().book_exists("KB1") is True


@pytest.mark.parametrize("status", [502, 503, 504])
@responses.activate
def test_a_502_503_or_504_answer_raises_provider_unreachable(status: int) -> None:
    """A 502, 503 or 504 answer is treated as an unavailable service (DFT-D41)."""
    responses.add(responses.GET, f"{BASE}/api/v1/books/KB1", status=status)
    with pytest.raises(ProviderUnreachable) as exc_info:
        _client().book_exists("KB1")
    assert str(exc_info.value) == f"Komga is not available: HTTP {status}"


@responses.activate
def test_a_500_answer_is_still_an_answer() -> None:
    """HTTP 500 is an answer, not an unavailable service."""
    responses.add(responses.GET, f"{BASE}/api/v1/books/KB1", status=500)
    assert _client().get_book("KB1") is None


@responses.activate
def test_an_unavailable_answer_is_logged_before_it_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unavailable answer (502/503/504) is logged at DEBUG before raising."""
    responses.add(responses.GET, f"{BASE}/api/v1/books/KB1", status=503)
    with (
        caplog.at_level(logging.DEBUG, logger="komga_sync.client"),
        pytest.raises(ProviderUnreachable),
    ):
        _client().book_exists("KB1")
    assert "Komga GET /api/v1/books/KB1 -> 503" in caplog.text


@responses.activate
def test_test_connection_reports_a_503_as_unreachable() -> None:
    """test_connection() reports a 503 answer as unreachable (caught by _request raising)."""
    responses.add(responses.GET, f"{BASE}/actuator/info", status=503)
    result = _client(library="").test_connection()
    assert result.outcome == "unreachable"
    assert result.message.startswith("Could not reach the Komga server at http://komga.test")


@responses.activate
def test_get_series() -> None:
    series = _fixture("series_0QJH5QMC8ES02.json")
    responses.add(responses.GET, f"{BASE}/api/v1/series/0QJH5QMC8ES02", json=series, status=200)
    assert _client().get_series("0QJH5QMC8ES02")["metadata"]["language"] == "en"


@responses.activate
def test_get_series_missing() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/series/X", status=404)
    assert _client().get_series("X") is None


@responses.activate
def test_trigger_analyze_ok_and_failure() -> None:
    responses.add(responses.POST, f"{BASE}/api/v1/books/B/analyze", status=202)
    assert _client().trigger_analyze("B") is True
    responses.add(responses.POST, f"{BASE}/api/v1/books/C/analyze", status=500)
    assert _client().trigger_analyze("C") is False


@responses.activate
def test_trigger_library_scan() -> None:
    responses.add(responses.POST, f"{BASE}/api/v1/libraries/{LIBRARY}/scan", status=202)
    assert _client().trigger_library_scan() is True


def test_trigger_library_scan_without_library_id() -> None:
    assert _client(library="").trigger_library_scan() is False


@responses.activate
def test_patch_book_metadata_sends_body() -> None:
    responses.add(responses.PATCH, f"{BASE}/api/v1/books/B/metadata", status=204)
    assert _client().patch_book_metadata("B", {"title": "X"}) is True
    assert json.loads(responses.calls[0].request.body) == {"title": "X"}


@responses.activate
def test_patch_series_metadata() -> None:
    responses.add(responses.PATCH, f"{BASE}/api/v1/series/S/metadata", status=204)
    assert _client().patch_series_metadata("S", {"language": "en"}) is True


@responses.activate
def test_patch_book_metadata_non_2xx_returns_false_and_redacts_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses.add(
        responses.PATCH, f"{BASE}/api/v1/books/B/metadata", status=400, body="bad request"
    )
    with caplog.at_level(logging.DEBUG, logger="komga_sync.client"):
        assert _client().patch_book_metadata("B", {"title": "X"}) is False
    assert "***" in caplog.text  # X-API-Key masked in the DEBUG header dump
    assert "secret-key" not in caplog.text  # the real key is never logged
    assert "bad request" in caplog.text  # full response body logged at DEBUG


@responses.activate
def test_patch_series_metadata_non_2xx_returns_false() -> None:
    responses.add(responses.PATCH, f"{BASE}/api/v1/series/S/metadata", status=500)
    assert _client().patch_series_metadata("S", {"language": "en"}) is False


@responses.activate
def test_patch_raises_provider_unreachable_on_a_network_error() -> None:
    """patch_book_metadata raises ProviderUnreachable when the request fails to send."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    responses.add(
        responses.PATCH, f"{BASE}/api/v1/books/B/metadata", body=requests.ConnectionError()
    )
    with pytest.raises(ProviderUnreachable):
        _client().patch_book_metadata("B", {"title": "X"})


@responses.activate
def test_get_progression() -> None:
    prog = _fixture("progression_0QJH5QMC8ES03.json")
    responses.add(responses.GET, f"{BASE}/api/v1/books/B/progression", json=prog, status=200)
    assert _client().get_progression("B")["locator"]["href"] == "OEBPS/file0001.xhtml"


@responses.activate
def test_get_progression_missing() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/B/progression", status=404)
    assert _client().get_progression("B") == {}


@responses.activate
def test_put_progression_ok_and_failure() -> None:
    prog = _fixture("progression_0QJH5QMC8ES03.json")
    responses.add(responses.PUT, f"{BASE}/api/v1/books/B/progression", status=204)
    assert _client().put_progression("B", prog) is True
    responses.add(responses.PUT, f"{BASE}/api/v1/books/C/progression", status=400)
    assert _client().put_progression("C", prog) is False


@responses.activate
def test_put_progression_failure_logs_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses.add(
        responses.PUT, f"{BASE}/api/v1/books/B/progression", status=400, body="bad progression"
    )
    with caplog.at_level(logging.DEBUG, logger="komga_sync.client"):
        assert _client().put_progression("B", {"locator": {}}) is False
    assert "***" in caplog.text  # X-API-Key masked in the DEBUG header dump
    assert "secret-key" not in caplog.text  # the real key is never logged
    assert "bad progression" in caplog.text  # full response body logged at DEBUG
    assert any(
        r.levelname == "ERROR" and "PUT" in r.message and "400" in r.message for r in caplog.records
    )


@responses.activate
def test_list_library_books_returns_id_url_pairs() -> None:
    """T7: list_library_books fetches GET /api/v1/books and returns (id, url) tuples."""
    responses.add(
        responses.GET,
        f"{BASE}/api/v1/books",
        json={
            "content": [
                {"id": "B1", "url": "/books/Auth/Story.epub"},
                {"id": "B2", "url": "/books/Auth/Other.epub"},
            ]
        },
        status=200,
    )
    result = _client().list_library_books()
    assert result == [("B1", "/books/Auth/Story.epub"), ("B2", "/books/Auth/Other.epub")]


@responses.activate
def test_list_library_books_sends_library_id_and_unpaged() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books", json={"content": []}, status=200)
    _client().list_library_books()
    url = responses.calls[0].request.url
    assert f"library_id={LIBRARY}" in url
    assert "unpaged=true" in url


def test_list_library_books_without_library_id_returns_empty() -> None:
    assert _client("").list_library_books() == []


@responses.activate
def test_list_library_books_server_error_returns_empty() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books", status=500)
    assert _client().list_library_books() == []


@responses.activate
def test_list_library_books_excludes_deleted_books() -> None:
    """Deleted books are excluded from the index."""
    responses.add(
        responses.GET,
        f"{BASE}/api/v1/books",
        json={
            "content": [
                {"id": "B1", "url": "/books/A/One.epub", "deleted": False},
                {"id": "B2", "url": "/books/A/Two.epub", "deleted": True},
            ]
        },
        status=200,
    )
    result = _client().list_library_books()
    assert result == [("B1", "/books/A/One.epub")]


@responses.activate
def test_list_library_books_logs_the_skipped_count_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skipped deleted books are logged at DEBUG level."""
    responses.add(
        responses.GET,
        f"{BASE}/api/v1/books",
        json={
            "content": [
                {"id": "B1", "url": "/books/A/One.epub", "deleted": False},
                {"id": "B2", "url": "/books/A/Two.epub", "deleted": True},
            ]
        },
        status=200,
    )
    with caplog.at_level(logging.DEBUG, logger="komga_sync.client"):
        _client().list_library_books()
    assert any(
        r.levelname == "DEBUG" and "skipped 1 book(s) marked deleted" in r.message
        for r in caplog.records
    )


@responses.activate
def test_list_library_books_logs_nothing_when_none_are_deleted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No log when there are no deleted books."""
    responses.add(
        responses.GET,
        f"{BASE}/api/v1/books",
        json={
            "content": [
                {"id": "B1", "url": "/books/A/One.epub"},
                {"id": "B2", "url": "/books/A/Two.epub"},
            ]
        },
        status=200,
    )
    with caplog.at_level(logging.DEBUG, logger="komga_sync.client"):
        result = _client().list_library_books()
    assert result == [("B1", "/books/A/One.epub"), ("B2", "/books/A/Two.epub")]
    assert not any("marked deleted" in r.message for r in caplog.records)


@responses.activate
def test_delete_book_file_calls_endpoint() -> None:
    responses.add(responses.DELETE, f"{BASE}/api/v1/books/B/file", status=204)
    assert _client().delete_book_file("B") is True
    assert responses.calls[0].request.method == "DELETE"
    assert responses.calls[0].request.url == f"{BASE}/api/v1/books/B/file"


@responses.activate
def test_delete_book_file_non_2xx_returns_false_and_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses.add(responses.DELETE, f"{BASE}/api/v1/books/B/file", status=404)
    with caplog.at_level(logging.DEBUG, logger="komga_sync.client"):
        assert _client().delete_book_file("B") is False
    assert any(
        r.levelname == "ERROR" and "DELETE" in r.message and "404" in r.message
        for r in caplog.records
    )


@pytest.mark.pins("EXP-243")
@responses.activate
def test_a_rejected_mutation_names_the_body_and_the_payload_on_the_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line on a rejected mutation names the response body and the request payload."""
    responses.add(
        responses.PUT,
        f"{BASE}/api/v1/books/B/progression",
        status=400,
        body='{"violations":[{"message":"nope"}]}',
    )
    with caplog.at_level(logging.ERROR, logger="komga_sync.client"):
        result = _client().put_progression("B", {"locator": {"href": "OEBPS/file0001.xhtml"}})
        assert result is False

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    error_message = error_records[0].message
    assert "violations" in error_message
    assert "nope" in error_message
    assert "OEBPS/file0001.xhtml" in error_message
    assert "PUT" in error_message
    assert "400" in error_message
    assert "payload=" in error_message


@responses.activate
def test_a_long_komga_error_body_is_clipped_on_the_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line clips a very long response body to 200 chars + '...'."""
    responses.add(
        responses.PUT,
        f"{BASE}/api/v1/books/B/progression",
        status=400,
        body="x" * 5000,
    )
    with caplog.at_level(logging.ERROR, logger="komga_sync.client"):
        assert _client().put_progression("B", {"locator": {}}) is False

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    error_message = error_records[0].message
    assert len(error_message) < 600
    assert ("x" * 200 + "...") in error_message


@responses.activate
def test_the_komga_api_key_never_reaches_the_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line never contains the API key, but the DEBUG header dump still redacts it."""
    responses.add(
        responses.PUT,
        f"{BASE}/api/v1/books/B/progression",
        status=400,
        body='{"violations":[{"message":"nope"}]}',
    )
    with caplog.at_level(logging.DEBUG, logger="komga_sync.client"):
        assert _client().put_progression("B", {"locator": {}}) is False

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    for record in error_records:
        assert "secret-key" not in record.message

    # The DEBUG header dump still redacts the key
    assert "***" in caplog.text


@responses.activate
def test_a_mutation_with_no_payload_still_logs_an_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line for a mutation with no payload uses '-' for the payload field."""
    responses.add(responses.DELETE, f"{BASE}/api/v1/books/B/file", status=404)
    with caplog.at_level(logging.ERROR, logger="komga_sync.client"):
        assert _client().delete_book_file("B") is False

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    error_message = error_records[0].message
    assert "DELETE" in error_message
    assert "404" in error_message
    assert "payload=-" in error_message


@responses.activate
def test_empty_trash_posts_to_library_endpoint() -> None:
    responses.add(responses.POST, f"{BASE}/api/v1/libraries/{LIBRARY}/empty-trash", status=204)
    assert _client().empty_trash() is True
    assert responses.calls[0].request.method == "POST"


def test_empty_trash_without_library_id_returns_false() -> None:
    assert _client(library="").empty_trash() is False


def test_client_satisfies_protocol() -> None:
    assert isinstance(_client(), KomgaClient)


@responses.activate
def test_request_logs_status_debug(caplog: pytest.LogCaptureFixture) -> None:
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    with caplog.at_level(logging.DEBUG, logger="komga_sync.client"):
        _client(library="").test_connection()
    assert any("Komga GET" in r.message and "-> 200" in r.message for r in caplog.records)


@responses.activate
def test_get_positions_returns_list() -> None:
    positions_payload = {
        "positions": [
            {
                "href": "OEBPS/file0001.xhtml",
                "type": "application/xhtml+xml",
                "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
            }
        ],
        "total": 1,
    }
    responses.add(
        responses.GET, f"{BASE}/api/v1/books/BID/positions", json=positions_payload, status=200
    )
    result = _client().get_positions("BID")
    assert result == positions_payload["positions"]
    assert len(result) == 1
    assert result[0]["href"] == "OEBPS/file0001.xhtml"


@responses.activate
def test_get_positions_empty_on_http_error() -> None:
    responses.add(responses.GET, f"{BASE}/api/v1/books/BID/positions", status=404)
    assert _client().get_positions("BID") == []


@responses.activate
def test_get_positions_raises_on_network_error() -> None:
    """get_positions raises ProviderUnreachable on a connection error."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    responses.add(
        responses.GET, f"{BASE}/api/v1/books/BID/positions", body=requests.RequestException()
    )
    with pytest.raises(ProviderUnreachable):
        _client().get_positions("BID")


@responses.activate
def test_get_positions_empty_on_malformed_payload() -> None:
    responses.add(
        responses.GET, f"{BASE}/api/v1/books/BID/positions", json={"total": 0}, status=200
    )
    assert _client().get_positions("BID") == []


@responses.activate
def test_scan_library_posts_scan() -> None:
    responses.add(responses.POST, f"{BASE}/api/v1/libraries/42/scan", status=202)
    assert _client().scan_library("42") is True
    assert responses.calls[0].request.method == "POST"
    assert responses.calls[0].request.url == f"{BASE}/api/v1/libraries/42/scan"


@responses.activate
def test_empty_trash_for_posts_empty_trash() -> None:
    responses.add(responses.POST, f"{BASE}/api/v1/libraries/42/empty-trash", status=204)
    assert _client().empty_trash_for("42") is True
    assert responses.calls[0].request.method == "POST"
    assert responses.calls[0].request.url == f"{BASE}/api/v1/libraries/42/empty-trash"


@responses.activate
def test_every_wrapper_raises_provider_unreachable() -> None:
    """All wrapper methods raise ProviderUnreachable when the request fails to send."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    client = _client()

    # find_book_id
    responses.add(responses.POST, f"{BASE}/api/v1/books/list", body=requests.ConnectionError())
    with pytest.raises(ProviderUnreachable):
        client.find_book_id("title", "author")

    # get_book
    responses.add(responses.GET, f"{BASE}/api/v1/books/B1", body=requests.ConnectionError())
    with pytest.raises(ProviderUnreachable):
        client.get_book("B1")

    # get_series
    responses.add(responses.GET, f"{BASE}/api/v1/series/S1", body=requests.ConnectionError())
    with pytest.raises(ProviderUnreachable):
        client.get_series("S1")

    # trigger_analyze
    responses.add(
        responses.POST, f"{BASE}/api/v1/books/B2/analyze", body=requests.ConnectionError()
    )
    with pytest.raises(ProviderUnreachable):
        client.trigger_analyze("B2")

    # get_progression
    responses.add(
        responses.GET, f"{BASE}/api/v1/books/B3/progression", body=requests.ConnectionError()
    )
    with pytest.raises(ProviderUnreachable):
        client.get_progression("B3")

    # get_positions
    responses.add(
        responses.GET, f"{BASE}/api/v1/books/B4/positions", body=requests.ConnectionError()
    )
    with pytest.raises(ProviderUnreachable):
        client.get_positions("B4")

    # list_library_books
    responses.add(responses.GET, f"{BASE}/api/v1/books", body=requests.ConnectionError())
    with pytest.raises(ProviderUnreachable):
        client.list_library_books()

    # delete_book_file
    responses.add(responses.DELETE, f"{BASE}/api/v1/books/B5/file", body=requests.ConnectionError())
    with pytest.raises(ProviderUnreachable):
        client.delete_book_file("B5")


@responses.activate
def test_a_body_cut_off_mid_way_is_provider_unreachable() -> None:
    """A ChunkedEncodingError (body cut off) raises ProviderUnreachable with the error name."""
    responses.add(
        responses.GET,
        f"{BASE}/api/v1/books/KB1",
        body=requests.exceptions.ChunkedEncodingError("cut"),
    )
    client = _client()
    with pytest.raises(ProviderUnreachable) as exc_info:
        client.book_exists("KB1")
    assert str(exc_info.value) == "Komga is not reachable: ChunkedEncodingError"


@responses.activate
def test_a_503_reads_could_not_reach() -> None:
    """A 503 on /actuator/info reads "Could not reach the Komga server at..." (DFT-D50)."""
    responses.add(responses.GET, f"{BASE}/actuator/info", status=503)
    result = _client(library="").test_connection()
    assert result.outcome == "unreachable"
    expected = (
        "Could not reach the Komga server at http://komga.test. "
        "Check the URL and that Komga is running."
    )
    assert result.message == expected


@responses.activate
def test_another_status_reads_answered_http() -> None:
    """A non-401/403/5xx status on /actuator/info reads "Komga answered HTTP..." (DFT-D50)."""
    responses.add(responses.GET, f"{BASE}/actuator/info", status=500)
    result = _client(library="").test_connection()
    assert result.outcome == "unreachable"
    assert result.message == "Komga answered HTTP 500."


@responses.activate
def test_komga_library_probe_losing_the_server_reads_could_not_reach() -> None:
    """A network error on library probe reads "Could not reach the Komga server at..." (DFT-D50)."""
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    responses.add(
        responses.GET, f"{BASE}/api/v1/libraries/{LIBRARY}", body=requests.ConnectionError()
    )
    result = _client().test_connection()
    assert result.outcome == "unreachable"
    expected = (
        "Could not reach the Komga server at http://komga.test. "
        "Check the URL and that Komga is running."
    )
    assert result.message == expected


@responses.activate
def test_komga_library_probe_other_status_reads_answered_http() -> None:
    """A non-401/403/404 status on library probe reads "Komga answered HTTP..." (DFT-D50)."""
    responses.add(responses.GET, f"{BASE}/actuator/info", json={"build": {}}, status=200)
    responses.add(responses.GET, f"{BASE}/api/v1/libraries/{LIBRARY}", status=500)
    result = _client().test_connection()
    assert result.outcome == "unreachable"
    assert result.message == "Komga answered HTTP 500."
