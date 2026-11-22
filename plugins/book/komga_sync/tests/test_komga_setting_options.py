"""TDD tests for the Komga Library id field choices (SPI 3.12)."""

from __future__ import annotations

import requests
import responses
from ebookerr_sdk.spi import SettingOptions
from komga_sync.plugin import KomgaSyncPlugin


@responses.activate
def test_the_libraries_are_listed_by_name() -> None:
    """The Library id field lists Komga's libraries, sorted by name, carrying the key."""
    responses.add(
        responses.GET,
        "http://k.test/api/v1/libraries",
        json=[{"id": "B", "name": "comics"}, {"id": "A", "name": "Books"}],
        status=200,
    )
    result = KomgaSyncPlugin().setting_options(
        "library_id", {"server": "http://k.test", "api_key": "k"}, object()
    )
    assert result == SettingOptions(options=("A", "B"), labels=("Books", "comics"))
    assert responses.calls[0].request.headers["X-API-Key"] == "k"


@responses.activate
def test_a_refused_key_says_so() -> None:
    """A 401 on the library listing says the key was refused."""
    responses.add(responses.GET, "http://k.test/api/v1/libraries", status=401)
    result = KomgaSyncPlugin().setting_options(
        "library_id", {"server": "http://k.test", "api_key": "k"}, object()
    )
    assert result.problem == "Komga refused the API key."


@responses.activate
def test_an_unreachable_server_says_so() -> None:
    """A connection error on the library listing says Komga is not reachable."""
    responses.add(responses.GET, "http://k.test/api/v1/libraries", body=requests.ConnectionError())
    result = KomgaSyncPlugin().setting_options(
        "library_id", {"server": "http://k.test", "api_key": "k"}, object()
    )
    assert result.problem == "Komga is not reachable."


@responses.activate
def test_missing_settings_say_what_to_enter() -> None:
    """Without an API key the field says to enter the server URL and the API key first."""
    result = KomgaSyncPlugin().setting_options("library_id", {"server": "http://k.test"}, object())
    assert result.problem == "Enter the server URL and the API key first."
