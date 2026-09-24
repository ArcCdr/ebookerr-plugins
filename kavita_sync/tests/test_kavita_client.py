"""Tests for the Kavita REST client (HTTP mocked with `responses`; no live calls)."""

from __future__ import annotations

import logging

import pytest
import requests
import responses
from ebookerr_sdk.providers.connection import ProviderUnreachable

from src.gateways.kavita_client import KavitaRef, RequestsKavitaClient
from src.gateways.protocols import KavitaClient

BASE = "http://kavita.test"
API_KEY = "test-api-key-12345"  # gitleaks:allow


def _client(session: requests.Session | None = None) -> RequestsKavitaClient:
    return RequestsKavitaClient(BASE, API_KEY, session=session)


@responses.activate
def test_test_connection_returns_connection_test_result() -> None:
    """test_connection returns ConnectionTestResult with ok outcome on successful token."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "ok"
    assert result.message == "Connected."
    assert result.ok is True


@responses.activate
def test_authenticate_returns_token() -> None:
    """Authenticate endpoint returns token; query carries apiKey + pluginName."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    client = _client()
    assert client._token() == "jwt123"

    # Verify request query params
    assert len(responses.calls) == 1
    request = responses.calls[0].request
    api_key_param = "apiKey=test-api-key-12345"  # gitleaks:allow
    assert api_key_param in request.url
    assert "pluginName=ebookerr" in request.url


@responses.activate
def test_token_cached() -> None:
    """Two _token() calls issue only one POST (cached)."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    client = _client()
    token1 = client._token()
    token2 = client._token()

    assert token1 == "jwt123"
    assert token2 == "jwt123"
    # Only one POST request made
    assert len(responses.calls) == 1


@responses.activate
def test_test_connection_rejected_on_401() -> None:
    """401 response -> test_connection returns (rejected, 'Kavita refused the API key.')."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "unauthorized"},
        status=401,
    )
    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "rejected"
    assert result.message == "Kavita refused the API key."
    assert result.ok is False


@responses.activate
def test_test_connection_rejected_on_403() -> None:
    """403 response -> test_connection returns (rejected, 'Kavita refused the API key.')."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "forbidden"},
        status=403,
    )
    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "rejected"
    assert result.message == "Kavita refused the API key."
    assert result.ok is False


@responses.activate
def test_test_connection_unreachable_on_network_error() -> None:
    """Network error -> test_connection returns (unreachable, 'Could not reach...')."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        body=requests.ConnectionError(),
    )
    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "unreachable"
    assert "Could not reach the Kavita server" in result.message
    assert BASE in result.message
    assert result.ok is False


@responses.activate
def test_test_connection_rejected_on_200_without_token() -> None:
    """200 without token field -> test_connection returns (rejected, 'refused the API key')."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "no token"},
        status=200,
    )
    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "rejected"
    assert result.message == "Kavita refused the API key."
    assert result.ok is False


@responses.activate
def test_test_connection_unreachable_on_500() -> None:
    """500 error -> test_connection returns (unreachable, f'Kavita answered HTTP 500.')."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "internal server error"},
        status=500,
    )
    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "unreachable"
    assert "Kavita answered HTTP 500" in result.message
    assert result.ok is False


@responses.activate
def test_auth_failure_returns_none_on_non_200() -> None:
    """Non-200 response -> _token() returns None."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "unauthorized"},
        status=401,
    )
    client = _client()
    assert client._token() is None


@responses.activate
def test_authentication_raises_provider_unreachable_on_a_network_error() -> None:
    """Network error during _token() raises ProviderUnreachable."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        body=requests.ConnectionError(),
    )
    client = _client()
    with pytest.raises(ProviderUnreachable) as exc_info:
        client._token()
    assert "Kavita is not reachable: ConnectionError" in str(exc_info.value)


@responses.activate
def test_auth_failure_returns_none_on_invalid_json() -> None:
    """Invalid JSON response -> _token() returns None."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        body="<<not json>>",
        status=200,
    )
    client = _client()
    assert client._token() is None


@responses.activate
def test_headers_bearer() -> None:
    """With a token, _headers() returns Bearer auth header."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    client = _client()
    # Call _token() to populate the cache
    assert client._token() == "jwt123"

    headers = client._headers()
    assert headers["Authorization"] == "Bearer jwt123"


@responses.activate
def test_headers_empty_without_token() -> None:
    """Without a token, _headers() returns empty dict."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "unauthorized"},
        status=401,
    )
    client = _client()
    # Auth failure, token is None
    assert client._token() is None

    headers = client._headers()
    assert headers == {}


@responses.activate
def test_api_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """API key is never logged, even at DEBUG level."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    client = _client()

    with caplog.at_level(logging.DEBUG):
        client._token()

    # API key should never appear in any log record
    for record in caplog.records:
        assert API_KEY not in record.getMessage()


@responses.activate
def test_api_key_never_in_test_connection_message() -> None:
    """API key is never included in test_connection result message."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "unauthorized"},
        status=401,
    )
    client = _client()
    result = client.test_connection()

    # API key should never appear in the message
    assert API_KEY not in result.message


def test_client_satisfies_protocol() -> None:
    """RequestsKavitaClient satisfies the KavitaClient protocol."""
    assert isinstance(_client(), KavitaClient)


@responses.activate
def test_find_chapter_matches_the_library_relative_path() -> None:
    """find_chapter matches a file whose path ends with the book's library-relative path."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - returns a chapter with matching file
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Foo/foo-story.epub",
                        }
                    ],
                }
            ],
            "series": [
                {
                    "seriesId": 5,
                    "libraryId": 3,
                }
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API with real DTO shape
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "id": 5,
            "libraryId": 3,
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("Foo/foo-story.epub")

    assert result is not None
    assert result.chapter_id == 42
    assert result.volume_id == 10
    assert result.series_id == 5
    assert result.library_id == 3
    assert result.total_pages == 150


@responses.activate
def test_find_chapter_matches_a_decomposed_server_path() -> None:
    """find_chapter matches a server path in decomposed Unicode against a composed name (TXE-D3)."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - the file path is decomposed Unicode (e + combining acute, etc.)
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/books/Zoë/Café.epub",
                        }
                    ],
                }
            ],
            "series": [
                {
                    "seriesId": 5,
                    "libraryId": 3,
                }
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API with real DTO shape
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "id": 5,
            "libraryId": 3,
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("Zoë/Café.epub")

    assert result is not None
    assert result.chapter_id == 42
    assert result.volume_id == 10
    assert result.series_id == 5
    assert result.library_id == 3
    assert result.total_pages == 150


@responses.activate
def test_find_chapter_case_insensitive() -> None:
    """find_chapter matches file basename case-insensitively."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - file has mixed case
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Foo/Foo-Story.EPUB",
                        }
                    ],
                }
            ],
            "series": [
                {
                    "seriesId": 5,
                    "libraryId": 3,
                }
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API with real DTO shape
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "id": 5,
            "libraryId": 3,
        },
        status=200,
    )

    client = _client()
    # Query with lowercase
    result = client.find_chapter("foo/foo-story.epub")

    assert result is not None
    assert result.chapter_id == 42


@responses.activate
@pytest.mark.pins("EXP-243")
def test_find_chapter_rejects_a_same_named_file_under_a_different_folder() -> None:
    """find_chapter rejects a file with matching basename but different path."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - file has the right basename but wrong directory
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Bar/foo-story.epub",
                        }
                    ],
                }
            ],
            "series": [],
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("Foo/foo-story.epub")

    assert result is None


@responses.activate
def test_find_chapter_picks_the_right_author_when_two_files_share_a_basename() -> None:
    """find_chapter selects the correct chapter when two files share a basename."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - two chapters with files sharing a basename
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Foo/foo-story.epub",
                        }
                    ],
                },
                {
                    "id": 43,
                    "volumeId": 11,
                    "pages": 150,
                    "files": [
                        {
                            "id": 100,
                            "filePath": "/data/lib/Bar/foo-story.epub",
                        }
                    ],
                },
            ],
            "series": [
                {
                    "seriesId": 5,
                    "libraryId": 3,
                },
                {
                    "seriesId": 6,
                    "libraryId": 3,
                },
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API for the Bar/foo-story.epub file
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "id": 6,
            "libraryId": 3,
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("Bar/foo-story.epub")

    assert result is not None
    assert result.chapter_id == 43


@responses.activate
def test_find_chapter_picks_the_right_author_whatever_the_result_order() -> None:
    """find_chapter selects the correct chapter regardless of result order."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - same two chapters but with order reversed
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 43,
                    "volumeId": 11,
                    "pages": 150,
                    "files": [
                        {
                            "id": 100,
                            "filePath": "/data/lib/Bar/foo-story.epub",
                        }
                    ],
                },
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Foo/foo-story.epub",
                        }
                    ],
                },
            ],
            "series": [
                {
                    "seriesId": 6,
                    "libraryId": 3,
                },
                {
                    "seriesId": 5,
                    "libraryId": 3,
                },
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API for the Bar/foo-story.epub file
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "id": 6,
            "libraryId": 3,
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("Bar/foo-story.epub")

    assert result is not None
    assert result.chapter_id == 43


@responses.activate
def test_a_basename_only_candidate_is_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """find_chapter logs at DEBUG when a file has the right basename but different path."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - file has the right basename but wrong directory
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Bar/foo-story.epub",
                        }
                    ],
                }
            ],
            "series": [],
        },
        status=200,
    )

    client = _client()
    with caplog.at_level(logging.DEBUG, logger="src.gateways.kavita_client"):
        result = client.find_chapter("Foo/foo-story.epub")

    assert result is None
    assert "has the right basename but a different path" in caplog.text
    assert "/data/lib/Bar/foo-story.epub" in caplog.text


@responses.activate
@pytest.mark.pins("EXP-190")
def test_series_for_mangafile_reads_the_dto_id_not_series_id() -> None:
    """series-for-mangafile returns SeriesDto with 'id' key, not 'seriesId'."""

    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - returns chapters with matching file
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/books/The Salt Ledger/The Salt Ledger - Vol. 3.epub",
                        }
                    ],
                }
            ],
            "series": [],
        },
        status=200,
    )
    # Mock series-for-mangafile API with real DTO shape: 'id', not 'seriesId'
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "id": 8,
            "libraryId": 1,
            "name": "The Salt Ledger",
            "originalName": "The Salt Ledger",
            "pages": 150,
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("The Salt Ledger/The Salt Ledger - Vol. 3.epub")

    assert isinstance(result, KavitaRef)
    assert result.series_id == 8
    assert result.library_id == 1
    assert result.chapter_id == 42


@responses.activate
def test_a_matched_file_whose_series_is_unresolved_is_not_none() -> None:
    """A file matched but series unresolved returns KavitaSeriesUnresolved, not None."""
    from src.gateways.kavita_client import KavitaSeriesUnresolved

    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - file matches but search series is empty
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/books/The Salt Ledger/The Salt Ledger - Vol. 3.epub",
                        }
                    ],
                }
            ],
            "series": [],
        },
        status=200,
    )
    # Mock series-for-mangafile API without an 'id' field
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "libraryId": 1,
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("The Salt Ledger/The Salt Ledger - Vol. 3.epub")

    assert isinstance(result, KavitaSeriesUnresolved)
    assert result.stem == "The Salt Ledger - Vol. 3.epub"
    assert "series-for-mangafile answered without an id" in result.detail
    assert "no series named 'The Salt Ledger - Vol. 3'" in result.detail
    assert result.message.startswith('"The Salt Ledger - Vol. 3.epub" is indexed in Kavita')


@responses.activate
def test_the_name_fallback_needs_exactly_one_series_named_like_the_stem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ambiguous fallback (2+ series with same name) returns KavitaSeriesUnresolved."""
    from src.gateways.kavita_client import KavitaSeriesUnresolved

    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - file matches, but search returns 2 series with same name
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/books/foo-story.epub",
                        }
                    ],
                }
            ],
            "series": [
                {"name": "foo-story", "seriesId": 5, "libraryId": 3},
                {"name": "foo-story", "seriesId": 6, "libraryId": 3},
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API returns 500
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        status=500,
    )

    client = _client()
    with caplog.at_level(logging.WARNING):
        result = client.find_chapter("foo-story.epub")

    assert isinstance(result, KavitaSeriesUnresolved)
    assert "2 series named 'foo-story'" in result.detail
    assert (
        "Kavita series lookup ambiguous for foo-story.epub: 2 series named 'foo-story'"
        in caplog.text
    )


@responses.activate
def test_the_name_fallback_resolves_a_unique_exact_name() -> None:
    """Unique name fallback returns KavitaRef with the single matching series."""
    from src.gateways.kavita_client import KavitaRef

    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - file matches, search returns 2 series but only 1 matches name
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/books/foo-story.epub",
                        }
                    ],
                }
            ],
            "series": [
                {"name": "foo-story", "seriesId": 5, "libraryId": 3},
                {"name": "other", "seriesId": 6, "libraryId": 3},
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API returns 500
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        status=500,
    )

    client = _client()
    result = client.find_chapter("foo-story.epub")

    assert isinstance(result, KavitaRef)
    assert result.series_id == 5
    assert result.library_id == 3


@responses.activate
def test_find_chapter_no_match_returns_none() -> None:
    """find_chapter returns None when no file matches the basename."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - file doesn't match
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Bar/different-file.epub",
                        }
                    ],
                }
            ],
            "series": [],
        },
        status=200,
    )

    client = _client()
    result = client.find_chapter("Foo/foo-story.epub")

    assert result is None


@responses.activate
def test_find_chapter_raises_provider_unreachable_on_a_transport_failure() -> None:
    """find_chapter raises ProviderUnreachable when search request fails."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - returns error
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        body=requests.ConnectionError(),
    )

    client = _client()
    with pytest.raises(ProviderUnreachable) as exc_info:
        client.find_chapter("Foo/foo-story.epub")
    assert "Kavita is not reachable: ConnectionError" in str(exc_info.value)


@responses.activate
def test_get_progress_returns_dto() -> None:
    """get_progress returns ProgressDto verbatim; error returns {}."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock get-progress endpoint
    progress_dto = {
        "chapterId": 42,
        "pageNumber": 75,
        "isRead": False,
    }
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        json=progress_dto,
        status=200,
    )

    client = _client()
    result = client.get_progress(42)

    assert result == progress_dto


@responses.activate
def test_get_progress_returns_empty_dict_on_error() -> None:
    """get_progress returns {} on non-200 status or exception."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        status=500,
    )

    client = _client()
    result = client.get_progress(42)

    assert result == {}


@responses.activate
def test_save_progress_posts_reader_progress() -> None:
    """save_progress builds the five-key body from a KavitaRef and returns True on 2xx."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        status=200,
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)
    result = client.save_progress(ref, 12)

    assert result is True
    # Verify the POST body
    assert len(responses.calls) == 2  # auth + save_progress
    request = responses.calls[1].request
    assert request.url is not None
    assert request.url.endswith("/api/Reader/progress")
    import json

    assert json.loads(request.body) == {
        "volumeId": 3,
        "chapterId": 7,
        "pageNum": 12,
        "seriesId": 5,
        "libraryId": 1,
    }


@responses.activate
def test_save_progress_non_2xx_returns_false() -> None:
    """save_progress returns False on a non-2xx response."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        status=500,
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)
    result = client.save_progress(ref, 12)

    assert result is False


@responses.activate
def test_save_progress_connection_error_raises_provider_unreachable() -> None:
    """save_progress raises ProviderUnreachable on a connection error."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        body=requests.ConnectionError(),
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)
    with pytest.raises(ProviderUnreachable):
        client.save_progress(ref, 12)


@responses.activate
def test_rate_series_posts_body() -> None:
    """rate_series POSTs body with seriesId and userRating, returns True on 2xx."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Rating/series",
        status=200,
    )

    client = _client()
    result = client.rate_series(7, 4.0)

    assert result is True
    # Verify the POST body
    assert len(responses.calls) == 2  # auth + rate_series
    request = responses.calls[1].request
    import json

    body = json.loads(request.body)
    assert body == {"seriesId": 7, "userRating": 4.0}


@responses.activate
def test_rate_series_returns_false_on_error() -> None:
    """rate_series returns False on error."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Rating/series",
        status=500,
    )

    client = _client()
    result = client.rate_series(7, 4.0)

    assert result is False


@responses.activate
def test_series_rating_reads_value() -> None:
    """series_rating returns the user's rating from the series entity; None if missing."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Series/7",
        json={"id": 7, "userRating": 4.5},
        status=200,
    )

    client = _client()
    result = client.series_rating(7)

    assert result == 4.5


@responses.activate
def test_series_rating_returns_none_on_error() -> None:
    """series_rating returns None on error or missing/zero rating."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Series/7",
        json={"id": 7, "userRating": 0},
        status=200,
    )

    client = _client()
    result = client.series_rating(7)

    assert result is None


@responses.activate
def test_series_rating_returns_none_on_http_error() -> None:
    """series_rating returns None on HTTP error."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Series/7",
        status=500,
    )

    client = _client()
    result = client.series_rating(7)

    assert result is None


@responses.activate
def test_scan_folder_posts_apikey_and_path() -> None:
    """scan_folder POSTs body with apiKey, folderPath, abortOnNoSeriesMatch, returns True on 2xx."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Library/scan-folder",
        status=200,
    )

    client = _client()
    result = client.scan_folder("/data/comics")

    assert result is True
    # Verify the POST body
    assert len(responses.calls) == 2  # auth + scan_folder
    request = responses.calls[1].request
    import json

    body = json.loads(request.body)
    assert body["apiKey"] == API_KEY
    assert body["folderPath"] == "/data/comics"
    assert body["abortOnNoSeriesMatch"] is False


@responses.activate
def test_scan_folder_returns_false_on_error() -> None:
    """scan_folder returns False on error."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Library/scan-folder",
        status=500,
    )

    client = _client()
    result = client.scan_folder("/data/comics")

    assert result is False


@responses.activate
def test_every_wrapper_raises_provider_unreachable_on_a_transport_failure() -> None:
    """Every wrapper raises ProviderUnreachable on a transport failure."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    # Mock auth to succeed
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # All endpoints raise ConnectionError
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        body=requests.ConnectionError(),
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        body=requests.ConnectionError(),
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Rating/series",
        body=requests.ConnectionError(),
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Series/7",
        body=requests.ConnectionError(),
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Library/scan-folder",
        body=requests.ConnectionError(),
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Book/7/chapters",
        body=requests.ConnectionError(),
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)

    # Each should raise ProviderUnreachable
    with pytest.raises(ProviderUnreachable):
        client.get_progress(42)
    with pytest.raises(ProviderUnreachable):
        client.save_progress(ref, 12)
    with pytest.raises(ProviderUnreachable):
        client.rate_series(7, 4.0)
    with pytest.raises(ProviderUnreachable):
        client.series_rating(7)
    with pytest.raises(ProviderUnreachable):
        client.scan_folder("/data/comics")
    with pytest.raises(ProviderUnreachable):
        client.book_chapters(7)


@responses.activate
def test_find_chapter_logs_match_debug(caplog: pytest.LogCaptureFixture) -> None:
    """find_chapter logs match at DEBUG level."""
    # Mock auth
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock search API - returns a chapter with matching file
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        json={
            "chapters": [
                {
                    "id": 42,
                    "volumeId": 10,
                    "pages": 150,
                    "files": [
                        {
                            "id": 99,
                            "filePath": "/data/lib/Foo/foo-story.epub",
                        }
                    ],
                }
            ],
            "series": [
                {
                    "seriesId": 5,
                    "libraryId": 3,
                }
            ],
        },
        status=200,
    )
    # Mock series-for-mangafile API with real DTO shape
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/series-for-mangafile",
        json={
            "id": 5,
            "libraryId": 3,
        },
        status=200,
    )

    client = _client()
    with caplog.at_level(logging.DEBUG):
        result = client.find_chapter("Foo/foo-story.epub")

    assert result is not None
    assert "Kavita chapter matched for" in caplog.text


@responses.activate
def test_save_progress_failure_logged_error(caplog: pytest.LogCaptureFixture) -> None:
    """save_progress logs error on non-2xx response."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        status=500,
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)
    with caplog.at_level(logging.ERROR):
        result = client.save_progress(ref, 12)

    assert result is False
    assert "Kavita save_progress failed" in caplog.text


@pytest.mark.pins("EXP-243")
@responses.activate
def test_a_rejected_save_progress_names_the_body_and_the_payload_on_the_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line on a rejected save_progress names the response body and the request payload."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        status=500,
        body='{"detail":"chapter not found"}',
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)
    with caplog.at_level(logging.ERROR, logger="src.gateways.kavita_client"):
        result = client.save_progress(ref, 12)

    assert result is False
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    error_message = error_records[0].message
    assert "Kavita save_progress failed" in error_message
    assert "chapter 7" in error_message
    assert "POST" in error_message
    assert "/api/Reader/progress" in error_message
    assert "chapter not found" in error_message
    assert '"pageNum": 12' in error_message
    assert "500" in error_message


@responses.activate
def test_a_rejected_rate_series_names_the_body_and_the_payload_on_the_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line on a rejected rate_series names the response body and the request payload."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Rating/series",
        status=400,
        body='{"detail":"bad rating"}',
    )

    client = _client()
    with caplog.at_level(logging.ERROR, logger="src.gateways.kavita_client"):
        result = client.rate_series(5, 4.0)

    assert result is False
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    error_message = error_records[0].message
    assert "Kavita rate_series failed" in error_message
    assert "series 5" in error_message
    assert "/api/Rating/series" in error_message
    assert "bad rating" in error_message
    assert '"userRating": 4.0' in error_message


@responses.activate
def test_a_long_kavita_error_body_is_clipped_on_the_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line clips a very long response body to 200 chars + '...'."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        status=500,
        body="y" * 5000,
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)
    with caplog.at_level(logging.ERROR, logger="src.gateways.kavita_client"):
        result = client.save_progress(ref, 12)

    assert result is False
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    error_message = error_records[0].message
    assert len(error_message) < 600
    assert ("y" * 200 + "...") in error_message


@responses.activate
def test_the_kavita_key_and_token_never_reach_the_error_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ERROR line never contains the API key or bearer token."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        status=500,
        body='{"detail":"chapter not found"}',
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)
    with caplog.at_level(logging.DEBUG, logger="src.gateways.kavita_client"):
        result = client.save_progress(ref, 12)

    assert result is False
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    for record in error_records:
        assert API_KEY not in record.message
        assert "jwt123" not in record.message


@responses.activate
def test_book_chapters_returns_list() -> None:
    """book_chapters returns list verbatim on 200; request URL ends /api/Book/X/chapters."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    toc_data = [
        {"title": "Title Page", "part": "", "page": 0, "children": []},
        {"title": "Ch 1", "part": "", "page": 3, "children": []},
    ]
    responses.add(
        responses.GET,
        f"{BASE}/api/Book/42/chapters",
        json=toc_data,
        status=200,
    )

    client = _client()
    result = client.book_chapters(42)

    assert result == toc_data
    # Verify request URL
    assert len(responses.calls) == 2  # auth + book_chapters
    request = responses.calls[1].request
    assert request.url is not None
    assert request.url.endswith("/api/Book/42/chapters")


@responses.activate
def test_book_chapters_empty_on_http_error() -> None:
    """book_chapters returns [] on non-200 HTTP status."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Book/42/chapters",
        status=500,
    )

    client = _client()
    result = client.book_chapters(42)

    assert result == []


@responses.activate
def test_book_chapters_raises_provider_unreachable_on_network_error() -> None:
    """book_chapters raises ProviderUnreachable on a connection error."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Book/42/chapters",
        body=requests.RequestException(),
    )

    client = _client()
    with pytest.raises(ProviderUnreachable):
        client.book_chapters(42)


@responses.activate
def test_book_chapters_empty_on_non_list() -> None:
    """book_chapters returns [] when 200 body is not a list."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Book/42/chapters",
        json={"oops": 1},
        status=200,
    )

    client = _client()
    result = client.book_chapters(42)

    assert result == []


@responses.activate
def test_a_non_2xx_answer_is_still_a_safe_default() -> None:
    """Non-2xx HTTP responses return safe defaults, not raise."""
    # Mock auth to succeed
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    # Mock all endpoints with 500 errors
    responses.add(
        responses.GET,
        f"{BASE}/api/Search/search",
        status=500,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        status=500,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Reader/progress",
        status=500,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Rating/series",
        status=500,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Series/7",
        status=500,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Library/scan-folder",
        status=500,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Book/7/chapters",
        status=500,
    )

    client = _client()
    ref = KavitaRef(chapter_id=7, volume_id=3, series_id=5, library_id=1, total_pages=40)

    # All should return safe defaults, not raise
    assert client.find_chapter("foo-story.epub") is None
    assert client.get_progress(42) == {}
    assert client.save_progress(ref, 12) is False
    assert client.rate_series(7, 4.0) is False
    assert client.series_rating(7) is None
    assert client.scan_folder("/data/comics") is False
    assert client.book_chapters(7) == []


@responses.activate
def test_scan_library_posts_the_library_id() -> None:
    """scan_library POSTs to /api/Library/scan with libraryId and force=false; 2xx -> True."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Library/scan",
        status=200,
    )
    client = _client()
    result = client.scan_library(1)

    assert result is True
    # Verify the request was made with correct params
    assert len(responses.calls) == 2
    request = responses.calls[1].request
    assert "libraryId=1" in request.url
    assert "force=false" in request.url


@responses.activate
def test_scan_library_returns_false_on_non_2xx() -> None:
    """scan_library returns False on non-2xx; logs WARNING."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/Library/scan",
        status=500,
    )
    client = _client()
    result = client.scan_library(1)

    assert result is False


@responses.activate
@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_502_503_or_504_answer_raises_provider_unreachable(status: int) -> None:
    """502/503/504 answers raise ProviderUnreachable (DFT-D41)."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        status=status,
    )

    client = _client()
    with pytest.raises(ProviderUnreachable) as exc_info:
        client.get_progress(42)

    assert str(exc_info.value) == f"Kavita is not available: HTTP {status}"


@responses.activate
def test_a_503_on_authenticate_is_not_cached() -> None:
    """503 on authenticate raises and does not cache False."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        status=503,
    )

    client = _client()
    with pytest.raises(ProviderUnreachable):
        client._token()

    assert client._token_cache is None


@responses.activate
def test_a_500_answer_is_still_an_answer() -> None:
    """500 answer is not raised; returns safe default (no exception)."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        status=500,
    )

    client = _client()
    result = client.get_progress(42)

    assert result == {}


@responses.activate
def test_an_unavailable_answer_is_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """502/503/504 answers are logged at DEBUG level before raising."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        status=503,
    )

    client = _client()
    caplog.set_level(logging.DEBUG, logger="src.gateways.kavita_client")
    with pytest.raises(ProviderUnreachable):
        client.get_progress(42)

    assert "Kavita GET /api/Reader/get-progress -> HTTP 503" in caplog.text
    assert API_KEY not in caplog.text


@responses.activate
def test_test_connection_still_reports_the_status() -> None:
    """test_connection reports a 503 as unreachable and never raises (DFT-D50)."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        status=503,
    )

    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "unreachable"
    expected = (
        "Could not reach the Kavita server at http://kavita.test. "
        "Check the URL and that Kavita is running."
    )
    assert result.message == expected


@responses.activate
def test_a_body_cut_off_mid_way_is_provider_unreachable() -> None:
    """A ChunkedEncodingError (body cut off) raises ProviderUnreachable with the error name."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/Reader/get-progress",
        body=requests.exceptions.ChunkedEncodingError("cut"),
    )
    client = _client()
    with pytest.raises(ProviderUnreachable) as exc_info:
        client.get_progress(42)
    assert "Kavita is not reachable: ChunkedEncodingError" in str(exc_info.value)


@responses.activate
def test_a_502_reads_could_not_reach() -> None:
    """test_connection reports a 502 as unreachable (DFT-D50)."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        status=502,
    )

    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "unreachable"
    expected = (
        "Could not reach the Kavita server at http://kavita.test. "
        "Check the URL and that Kavita is running."
    )
    assert result.message == expected


@responses.activate
def test_another_status_reads_answered_http() -> None:
    """test_connection reports a 500 status as answered (DFT-D50)."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"error": "x"},
        status=500,
    )

    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "unreachable"
    assert result.message == "Kavita answered HTTP 500."


@responses.activate
def test_a_non_json_token_answer_reads_could_not_reach() -> None:
    """test_connection reports a 200 with non-JSON as unreachable (DFT-D50)."""
    from ebookerr_sdk.providers.connection import ConnectionTestResult

    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        body="<html>",
        status=200,
    )

    client = _client()
    result = client.test_connection()

    assert isinstance(result, ConnectionTestResult)
    assert result.outcome == "unreachable"
    expected = (
        "Could not reach the Kavita server at http://kavita.test. "
        "Check the URL and that Kavita is running."
    )
    assert result.message == expected


@responses.activate
def test_the_connection_test_never_logs_the_key(caplog: pytest.LogCaptureFixture) -> None:
    """test_connection does not log the API key, even on exception (DFT-D50)."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        body=requests.ConnectionError(),
    )

    client = _client()
    caplog.set_level(logging.DEBUG)
    result = client.test_connection()

    assert result.outcome == "unreachable"
    assert API_KEY not in caplog.text
