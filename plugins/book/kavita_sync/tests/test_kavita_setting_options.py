"""TDD tests for the Kavita Library folder field choices (SPI 3.12)."""

from __future__ import annotations

import requests
import responses
from ebookerr_sdk.spi import SettingOptions
from kavita_sync.plugin import KavitaSyncPlugin

BASE = "http://v.test"
API_KEY = "test-api-key-12345"  # gitleaks:allow


def _authenticate() -> None:
    """Answer the token exchange, as test_kavita_client.py does."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        json={"token": "jwt123"},
        status=200,
    )


@responses.activate
def test_each_library_folder_is_listed_with_its_library() -> None:
    """Each library's folders are listed as (folder, "Library — folder"), by label.

    A folder two libraries share carries the first library's label.
    """
    _authenticate()
    responses.add(
        responses.GET,
        f"{BASE}/api/Library/libraries",
        json=[
            {"id": 1, "name": "Books", "folders": ["/books"]},
            {"id": 2, "name": "Comics", "folders": ["/comics", "/books"]},
        ],
        status=200,
    )
    result = KavitaSyncPlugin().setting_options(
        "library_path", {"server": BASE, "api_key": API_KEY}, object()
    )
    assert result == SettingOptions(
        options=("/books", "/comics"), labels=("Books — /books", "Comics — /comics")
    )


@responses.activate
def test_a_refused_key_says_so() -> None:
    """A refused token says the API key was refused."""
    responses.add(responses.POST, f"{BASE}/api/Plugin/authenticate", status=401)
    result = KavitaSyncPlugin().setting_options(
        "library_path", {"server": BASE, "api_key": API_KEY}, object()
    )
    assert result.problem == "Kavita refused the API key."


@responses.activate
def test_an_unreachable_server_says_so() -> None:
    """A connection error on the token call says Kavita is not reachable."""
    responses.add(
        responses.POST,
        f"{BASE}/api/Plugin/authenticate",
        body=requests.ConnectionError(),
    )
    result = KavitaSyncPlugin().setting_options(
        "library_path", {"server": BASE, "api_key": API_KEY}, object()
    )
    assert result.problem == "Kavita is not reachable."


@responses.activate
def test_missing_settings_say_what_to_enter() -> None:
    """Without an API key the field says to enter the server URL and the API key first."""
    result = KavitaSyncPlugin().setting_options("library_path", {"server": BASE}, object())
    assert result.problem == "Enter the server URL and the API key first."
