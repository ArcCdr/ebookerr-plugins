"""Tests for the Literotica Stories catalog plugin."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import tomllib
import urllib.parse
from pathlib import Path

import pytest

# Dynamically import entrypoint module
_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "entrypoint.py"
_SPEC = importlib.util.spec_from_file_location("literotica_stories_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["literotica_stories_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "search_page.json"
_FIXTURE_DATA = json.loads(_FIXTURE_PATH.read_text())


def test_parse_search_url_full() -> None:
    """parse_search_url with all parameters returns correct dict and page."""
    result_params, result_page = _MODULE.parse_search_url(
        "https://search.literotica.com/?query=a&page=2&categories=29&period=1%20month&popular=true&sort=vote"
    )
    assert result_page == 2
    assert result_params == {
        "q": "a",
        "categories": [29],
        "period": "1 month",
        "popular": True,
        "sort": "vote",
        "languages": [1],
    }


def test_parse_search_url_defaults() -> None:
    """parse_search_url with no parameters uses defaults."""
    result_params, result_page = _MODULE.parse_search_url("https://search.literotica.com/")
    assert result_page == 1
    assert result_params == {"q": "", "languages": [1]}


def test_parse_search_url_multi_values() -> None:
    """parse_search_url with comma-separated values splits them."""
    result_params, result_page = _MODULE.parse_search_url(
        "https://search.literotica.com/?categories=29,12&tags=a,b"
    )
    assert result_params["categories"] == [29, 12]
    assert result_params["tags"] == ["a", "b"]
    assert result_page == 1


def test_unknown_params_listed() -> None:
    """unknown_params returns sorted list of unknown query keys."""
    result = _MODULE.unknown_params("https://search.literotica.com/?query=x&foo=1&bar=2")
    assert result == ["bar", "foo"]


def test_build_api_url_encodes_compact_json() -> None:
    """build_api_url produces correctly encoded JSON params URL."""
    result = _MODULE.build_api_url({"q": "a", "languages": [1]}, 2)
    expected_json = json.dumps({"q": "a", "languages": [1], "page": 2}, separators=(",", ":"))
    expected_url = _MODULE.API_URL + "?params=" + urllib.parse.quote(expected_json)
    assert result == expected_url


def test_map_story_core_fields() -> None:
    """map_story returns correct core StoryPatch fields."""
    story_a = _FIXTURE_DATA["data"][0]
    result = _MODULE.map_story(story_a)
    assert result["url"] == "https://www.literotica.com/s/not-another-spiral-story"
    assert result["title"] == "Not Another Spiral Story"
    assert result["author"] == "4SomeoneSpecial"
    assert result["author_url"] == "https://www.literotica.com/authors/4SomeoneSpecial"
    assert result["category"] == "Mind Control"
    assert result["tags"] == "wlw, hypnosis"
    assert result["rating"] == 4.52
    assert result["num_words"] == 2999
    assert result["date_published"] == "2026-07-03"
    assert result["story_id"] == "4450510"
    assert result["site"] == "literotica.com"
    assert "series" not in result
    assert "series_url" not in result
    # Ensure no named vote/views/favorites/comments/language keys (only in custom)
    assert "votes" not in result
    assert "views" not in result
    assert "favorites" not in result
    assert "comments" not in result
    assert "language" not in result


def test_get_tags_string_stringifies_a_numeric_tag() -> None:
    """A tag whose ``tag`` value is a raw JSON number (e.g. the "69" slang tag Literotica's
    API sends as an int, not a string) is stringified, not dropped or crashed on."""
    tags = [
        {"id": 843, "tag": "breasts", "is_banned": 0},
        {"id": 213, "tag": 69, "is_banned": 0},
    ]
    assert _MODULE._get_tags_string(tags) == "breasts, 69"


def test_map_story_custom_labels() -> None:
    """map_story custom labels match expected keys and values."""
    story_a = _FIXTURE_DATA["data"][0]
    result = _MODULE.map_story(story_a)
    assert result["custom"] == {
        "Votes": 31,
        "Views": 4204,
        "Favorites": 9,
        "Comments": 1,
        "Language": "en",
        "Type": "story",
        "Is Hot": True,
        "Is New": False,
        "Writer's Pick": False,
        "Contest Winner": False,
        "Reading Lists": 11,
        "Downloadable": False,
        "Author Stories": 12,
    }


def test_map_story_series() -> None:
    """map_story with series dict extracts series fields."""
    story_b = _FIXTURE_DATA["data"][1]
    result = _MODULE.map_story(story_b)
    assert result["series"] == "Bad Mom And Naughty Shrink"
    assert result["series_url"] == "https://www.literotica.com/series/se/495401236"
    assert result["custom"]["Series Parts"] == 2


def test_map_story_without_url_returns_none() -> None:
    """map_story returns None when url is missing or falsy."""
    result = _MODULE.map_story({"title": "x"})
    assert result is None


def test_apply_thresholds_and_semantics() -> None:
    """apply_thresholds keeps story only when all thresholds pass."""
    stories = [
        {"rating": 4.6, "custom": {"Is Hot": True}},
        {"rating": 4.6, "custom": {"Is Hot": False}},
        {"rating": 4.0, "custom": {"Is Hot": True}},
    ]
    thresholds = {"rating": 4.5, "Is Hot": 1}
    result = _MODULE.apply_thresholds(stories, thresholds)
    assert len(result) == 1
    assert result[0]["rating"] == 4.6
    assert result[0]["custom"]["Is Hot"] is True


def test_apply_thresholds_missing_field_drops() -> None:
    """apply_thresholds drops stories with missing threshold fields."""
    stories = [{"title": "x"}]
    result = _MODULE.apply_thresholds(stories, {"rating": 4})
    assert result == []

    # String value in custom field should also drop
    stories = [{"custom": {"Rank": "top"}}]
    result = _MODULE.apply_thresholds(stories, {"Rank": 1})
    assert result == []


def test_apply_thresholds_empty_keeps_all() -> None:
    """apply_thresholds with empty thresholds keeps all stories."""
    stories = [{"title": "x"}, {"title": "y"}, {"title": "z"}]
    result = _MODULE.apply_thresholds(stories, {})
    assert result == stories


def test_scan_fetches_and_maps(monkeypatch) -> None:
    """scan fetches and maps stories, returns result and logs."""

    def fake_fetch(url: str) -> dict:
        return _FIXTURE_DATA

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)
    assert len(stories) == 2
    assert all(isinstance(s, dict) for s in stories)

    # Check for info log about fetched/kept
    info_logs = [entry for entry in logs if entry["level"] == "info"]
    assert any("fetched 1 page(s), 2 stories, kept 2" in entry["message"] for entry in info_logs)

    # Check for final scan complete log
    assert any(
        "Scan complete: 2 unique stories from 1 URL(s)" in entry["message"] for entry in info_logs
    )


def test_scan_paginates_until_short_page(monkeypatch) -> None:
    """scan stops paging when a page has fewer than PAGE_SIZE stories."""
    call_list = []

    def fake_fetch(url: str) -> dict:
        call_list.append(url)
        if len(call_list) == 1:
            # First page: 50 stories
            return {
                "data": [
                    {
                        "url": f"story-{i}",
                        "title": f"Story {i}",
                        "date_approve": "07/01/2026",
                    }
                    for i in range(50)
                ]
            }
        else:
            # Second page: 2 stories (short, should stop)
            return {
                "data": [
                    {"url": "story-50", "title": "Story 50", "date_approve": "07/01/2026"},
                    {"url": "story-51", "title": "Story 51", "date_approve": "07/01/2026"},
                ]
            }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a"],
        "max_pages": 3,
    }
    stories, logs = _MODULE.scan(settings)
    assert len(call_list) == 2
    assert len(stories) == 52


def test_scan_respects_max_pages(monkeypatch) -> None:
    """scan stops after max_pages even if all pages are full."""
    call_list = []

    def fake_fetch(url: str) -> dict:
        call_list.append(url)
        # Always return a full 50-story page
        return {
            "data": [
                {
                    "url": f"story-p{len(call_list)}-{i}",
                    "title": f"Story P{len(call_list)} S{i}",
                    "date_approve": "07/01/2026",
                }
                for i in range(50)
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a"],
        "max_pages": 2,
    }
    stories, logs = _MODULE.scan(settings)
    assert len(call_list) == 2


def test_scan_start_page_from_url(monkeypatch) -> None:
    """scan starts paging from URL's page parameter."""
    call_list = []

    def fake_fetch(url: str) -> dict:
        call_list.append(url)
        # Always return a full page to prevent short-circuit
        return {
            "data": [
                {
                    "url": f"story-{len(call_list)}-{i}",
                    "title": f"Story {len(call_list)} {i}",
                    "date_approve": "07/01/2026",
                }
                for i in range(50)
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a&page=3"],
        "max_pages": 2,
    }
    stories, logs = _MODULE.scan(settings)

    # Both URLs passed to fake should contain page 3 and 4
    assert "%22page%22%3A3" in call_list[0]
    assert "%22page%22%3A4" in call_list[1]


def test_scan_thresholds_filter(monkeypatch) -> None:
    """scan applies thresholds and logs kept count."""

    def fake_fetch(url: str) -> dict:
        return _FIXTURE_DATA

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a"],
        "max_pages": 1,
        "min_thresholds": {"rating": 4.6},
    }
    stories, logs = _MODULE.scan(settings)

    # Fixture stories both have rating 4.52, which is < 4.6, so 0 kept
    assert len(stories) == 0

    # Check for info log about kept count
    info_logs = [entry for entry in logs if entry["level"] == "info"]
    assert any("kept 0 after thresholds" in entry["message"] for entry in info_logs)


def test_scan_dedupes_across_urls(monkeypatch) -> None:
    """scan deduplicates stories by URL across multiple search URLs."""

    def fake_fetch(url: str) -> dict:
        return _FIXTURE_DATA

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": [
            "https://search.literotica.com/?query=a",
            "https://search.literotica.com/?query=b",
        ],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    # Both URLs return the same 2 stories, but they should be deduplicated
    assert len(stories) == 2

    # Check final log
    info_logs = [entry for entry in logs if entry["level"] == "info"]
    assert any(
        "Scan complete: 2 unique stories from 2 URL(s)" in entry["message"] for entry in info_logs
    )


def test_scan_raises_when_the_first_page_fails(monkeypatch) -> None:
    """scan raises RuntimeError when the first page fetch fails."""
    import urllib.error

    def fake_fetch(url: str) -> dict:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a"],
        "max_pages": 1,
    }

    with pytest.raises(RuntimeError) as exc_info:
        _MODULE.scan(settings)

    # Check that error message contains "page"
    assert "page" in str(exc_info.value).lower()


def test_scan_raises_on_a_mid_paging_failure(monkeypatch) -> None:
    """scan raises RuntimeError when a mid-paging fetch fails."""
    call_count = [0]

    def fake_fetch(url: str) -> dict:
        call_count[0] += 1
        if call_count[0] == 1:
            # First page: full page (50 stories)
            return {
                "data": [
                    {
                        "url": f"story-{i}",
                        "title": f"Story {i}",
                        "date_approve": "07/01/2026",
                    }
                    for i in range(50)
                ]
            }
        else:
            # Second page: raise
            raise OSError("connection lost")

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a"],
        "max_pages": 2,
    }

    with pytest.raises(RuntimeError):
        _MODULE.scan(settings)


def test_scan_raises_when_one_of_two_urls_fails(monkeypatch) -> None:
    """scan raises RuntimeError when one of multiple URLs fails (not short listing)."""
    call_count = [0]

    def fake_fetch(url: str) -> dict:
        call_count[0] += 1
        if call_count[0] == 1:
            # First URL succeeds
            return _FIXTURE_DATA
        else:
            # Second URL fails
            raise OSError("boom")

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": [
            "https://search.literotica.com/?query=a",
            "https://search.literotica.com/?query=b",
        ],
        "max_pages": 1,
    }

    with pytest.raises(RuntimeError):
        _MODULE.scan(settings)


def test_scan_with_an_empty_listing_is_a_successful_empty_scan(monkeypatch) -> None:
    """scan returns empty result when page 1 has no data, without raising."""

    def fake_fetch(url: str) -> dict:
        return {"data": []}

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    # Should return empty list without raising
    assert stories == []
    # Should have logs
    assert len(logs) > 0


def test_scan_warns_on_unknown_params(monkeypatch) -> None:
    """scan warns when URL has unknown search parameters."""

    def fake_fetch(url: str) -> dict:
        return _FIXTURE_DATA

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=a&foo=1"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    # Check for warning log about unknown params
    warn_logs = [entry for entry in logs if entry["level"] == "warning"]
    assert any("ignoring unknown search parameters: foo" in entry["message"] for entry in warn_logs)


def test_fetch_json_retries_once_on_429(monkeypatch) -> None:
    """fetch_json retries once on 429, sleeps with _RETRY_DELAY_S."""
    import io
    import urllib.error

    call_count = [0]
    sleep_calls = []
    stdin_responses = [
        '{"circuit": {"open": false}}\n',  # is_open response
        '{"circuit": {"open": false}}\n',  # first record response (429)
        '{"circuit": {"open": false}}\n',  # second record response (success)
    ]
    stdin_iter = iter(stdin_responses)

    def fake_urlopen(request, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise urllib.error.HTTPError("url", 429, "slow down", {}, None)
        # Return a fake response object
        response = io.BytesIO(b'{"data": [], "meta": {}}')
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *args: None
        return response

    def fake_sleep(duration):
        sleep_calls.append(duration)

    def fake_stdin_readline():
        return next(stdin_iter, "")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(_MODULE.time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.stdin.readline", fake_stdin_readline)

    result = _MODULE.fetch_json("https://example.com")
    assert result == {"data": [], "meta": {}}
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == _MODULE._RETRY_DELAY_S


def test_main_scan_roundtrip(monkeypatch) -> None:
    """main reads scan request, calls scan, outputs JSON response."""

    def fake_fetch(url: str) -> dict:
        return _FIXTURE_DATA

    stdin_data = json.dumps(
        {
            "spi_version": "2.0",
            "op": "scan",
            "request": {"settings": {"search_urls": ["https://search.literotica.com/?query=a"]}},
        }
    )
    captured_stdout = []

    def fake_print(*args, **kwargs):
        captured_stdout.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)
    monkeypatch.setattr("builtins.input", lambda: stdin_data)
    monkeypatch.setattr("builtins.print", fake_print)

    _MODULE.main()

    # The final response is the one starting with {"ok"...
    response_output = next((line for line in captured_stdout if line.startswith('{"ok"')), None)
    assert response_output is not None, f"No response found in {captured_stdout}"
    parsed = json.loads(response_output)
    assert parsed["ok"] is True
    assert len(parsed["result"]) == 2
    assert isinstance(parsed["logs"], list)


def test_main_bad_op_reports_error(monkeypatch) -> None:
    """main reports error for unsupported operation."""

    stdin_data = json.dumps(
        {
            "spi_version": "2.0",
            "op": "pull",
            "request": {"settings": {}},
        }
    )
    captured_stdout = []

    def fake_print(*args, **kwargs):
        captured_stdout.append(" ".join(str(a) for a in args))

    monkeypatch.setattr("builtins.input", lambda: stdin_data)
    monkeypatch.setattr("builtins.print", fake_print)

    with contextlib.suppress(SystemExit):
        _MODULE.main()

    output = captured_stdout[0]
    parsed = json.loads(output)
    assert parsed["ok"] is False
    assert "unsupported operation" in parsed["error"]


def test_story_url_prefix_by_type() -> None:
    """story_url selects correct prefix by type."""
    assert _MODULE.story_url("story", "x") == "https://www.literotica.com/s/x"
    assert _MODULE.story_url("audio", "x") == "https://www.literotica.com/s/x"
    assert _MODULE.story_url("poem", "x") == "https://www.literotica.com/p/x"
    assert _MODULE.story_url("illustration", "x") == "https://www.literotica.com/i/x"


def test_story_url_unknown_type_falls_back_to_s() -> None:
    """story_url falls back to /s/ for unknown or missing types."""
    assert _MODULE.story_url("sgs", "x") == "https://www.literotica.com/s/x"
    assert _MODULE.story_url(None, "x") == "https://www.literotica.com/s/x"


def test_map_story_poem_uses_the_poem_url() -> None:
    """map_story with type=poem uses /p/ URL and sets Type custom field."""
    raw = {"url": "an-ode", "id": 42, "type": "poem", "title": "An Ode"}
    result = _MODULE.map_story(raw)
    assert result["url"] == "https://www.literotica.com/p/an-ode"
    assert result["custom"]["Type"] == "poem"


def test_map_story_without_type_still_uses_s() -> None:
    """map_story without type uses /s/ URL and omits Type from custom."""
    raw = {"url": "x", "id": 1}
    result = _MODULE.map_story(raw)
    assert result["url"] == "https://www.literotica.com/s/x"
    assert "Type" not in result.get("custom", {})


def test_manifest_threshold_options_strict() -> None:
    """Real manifest → the min_thresholds field has options_strict is True and correct 14-tuple."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    manifest_data = tomllib.loads(manifest_path.read_text())

    from ebookerr_sdk.spi.manifest import parse_manifest

    manifest = parse_manifest(manifest_data)

    # Find the min_thresholds field in settings_schema
    min_thresholds_field = None
    for field in manifest.settings_schema.fields:
        if field.key == "min_thresholds":
            min_thresholds_field = field
            break

    assert min_thresholds_field is not None, "min_thresholds field not found"
    assert min_thresholds_field.options_strict is True
    assert min_thresholds_field.options == (
        "rating",
        "num_words",
        "Votes",
        "Views",
        "Favorites",
        "Comments",
        "Rank",
        "Reading Lists",
        "Series Parts",
        "Author Stories",
        "Is Hot",
        "Is New",
        "Writer's Pick",
        "Contest Winner",
    )


def test_manifest_spi_2_1() -> None:
    """Real manifest → spi_version == '2.30', version == '2.2.0'."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    manifest_data = tomllib.loads(manifest_path.read_text())

    from ebookerr_sdk.spi.manifest import parse_manifest

    manifest = parse_manifest(manifest_data)

    assert manifest.spi_version == "2.30"
    assert manifest.version == "2.2.0"


def test_report_progress_writes_a_progress_frame(capsys) -> None:
    """_report_progress emits a progress frame JSON on stdout."""
    _MODULE._report_progress(42.5)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == {"op": "progress", "percent": 42.5}


def test_literotica_scan_reports_once_per_search_url(monkeypatch, capsys) -> None:
    """scan reports progress once per search URL with increasing percents."""

    def fake_fetch(url: str) -> dict:
        # Return a single short page (fewer items than PAGE_SIZE)
        return {
            "data": [
                {
                    "url": "story-1",
                    "title": "Story 1",
                    "date_approve": "07/01/2026",
                }
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["u1", "u2", "u3", "u4"],
        "max_pages": 1,
    }
    _MODULE.scan(settings)

    captured = capsys.readouterr()
    lines = [line.strip() for line in captured.out.split("\n") if line.strip()]
    progress_frames = [json.loads(line) for line in lines if line.startswith('{"op": "progress"')]

    percents = [frame["percent"] for frame in progress_frames]

    assert pytest.approx(percents) == [25.0, 50.0, 75.0, 100.0]


def test_literotica_scan_with_no_urls_emits_no_progress(capsys) -> None:
    """scan with empty search_urls list emits no progress frame."""
    settings = {"search_urls": []}
    _MODULE.scan(settings)

    captured = capsys.readouterr()
    assert '{"op": "progress"' not in captured.out


def test_category_name_maps_the_reported_examples() -> None:
    """category_name maps known integer IDs to Literotica display names."""
    assert _MODULE.category_name({"category": 13}) == "Reluctance/NonConsent"
    assert _MODULE.category_name({"category": 12}) == "Loving Wives"
    assert _MODULE.category_name({"category": 10}) == "Interracial Love"
    assert _MODULE.category_name({"category": 38}) == "Sci-Fi & Fantasy"


def test_category_name_accepts_a_string_id() -> None:
    """category_name coerces string category ID to int."""
    assert _MODULE.category_name({"category": "38"}) == "Sci-Fi & Fantasy"


def test_category_name_returns_none_for_an_unknown_id() -> None:
    """category_name returns None for unmapped category ID."""
    assert _MODULE.category_name({"category": 999}) is None


def test_category_name_returns_none_when_absent() -> None:
    """category_name returns None when category key is missing or None."""
    assert _MODULE.category_name({}) is None
    assert _MODULE.category_name({"category": None}) is None


def test_category_label_falls_back_to_the_slug() -> None:
    """category_label uses title-cased slug when ID is unmapped."""
    assert (
        _MODULE.category_label({"category": 999, "category_info": {"pageUrl": "brand-new-thing"}})
        == "Brand New Thing"
    )


def test_category_label_returns_none_without_id_or_slug() -> None:
    """category_label returns None when both ID and slug are absent."""
    assert _MODULE.category_label({}) is None


def test_category_label_prefers_the_mapped_name_over_the_slug() -> None:
    """category_label uses mapped name even when slug differs."""
    assert (
        _MODULE.category_label(
            {"category": 13, "category_info": {"pageUrl": "non-consent-stories"}}
        )
        == "Reluctance/NonConsent"
    )


def test_map_story_uses_the_mapped_category() -> None:
    """map_story applies the mapped category name from category_label."""
    raw = dict(
        _FIXTURE_DATA["data"][0],
        category=13,
        category_info={"type": "story", "pageUrl": "non-consent-stories"},
    )
    result = _MODULE.map_story(raw)
    assert result["category"] == "Reluctance/NonConsent"


def test_map_story_omits_category_when_unresolvable() -> None:
    """map_story omits category when neither ID nor slug is present."""
    raw = {
        k: v for k, v in _FIXTURE_DATA["data"][0].items() if k not in ("category", "category_info")
    }
    result = _MODULE.map_story(raw)
    assert "category" not in result


def test_category_table_has_forty_entries() -> None:
    """_CATEGORY_NAMES table has exactly 40 entries."""
    assert len(_MODULE._CATEGORY_NAMES) == 40


def test_category_table_values_are_non_empty_strings() -> None:
    """_CATEGORY_NAMES values are all non-empty strings."""
    for value in _MODULE._CATEGORY_NAMES.values():
        assert isinstance(value, str)
        assert value.strip()


def test_scan_warns_once_for_an_unmapped_category(monkeypatch) -> None:
    """scan emits exactly one aggregated warning for an unmapped category."""

    def fake_fetch(url: str) -> dict:
        return {
            "data": [
                {
                    "url": "a",
                    "category": 999,
                    "category_info": {"type": "story", "pageUrl": "brand-new"},
                },
                {
                    "url": "b",
                    "category": 999,
                    "category_info": {"type": "story", "pageUrl": "brand-new"},
                },
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=x"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    warn_logs = [entry for entry in logs if entry["level"] == "warning"]
    unmapped_warnings = [
        entry
        for entry in warn_logs
        if "Literotica category id(s) are not in this plugin's name table" in entry["message"]
    ]

    assert len(unmapped_warnings) == 1
    assert "1 Literotica category id(s)" in unmapped_warnings[0]["message"]
    assert "999=brand-new" in unmapped_warnings[0]["message"]


def test_scan_lists_every_unmapped_pair(monkeypatch) -> None:
    """scan lists all unique (id, slug) pairs in a single warning."""

    def fake_fetch(url: str) -> dict:
        return {
            "data": [
                {
                    "url": "a",
                    "category": 999,
                    "category_info": {"type": "story", "pageUrl": "brand-new"},
                },
                {
                    "url": "b",
                    "category": 999,
                    "category_info": {"type": "story", "pageUrl": "brand-new"},
                },
                {
                    "url": "c",
                    "category": 1000,
                    "category_info": {"type": "story", "pageUrl": "other-new"},
                },
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=x"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    warn_logs = [entry for entry in logs if entry["level"] == "warning"]
    unmapped_warnings = [
        entry
        for entry in warn_logs
        if "Literotica category id(s) are not in this plugin's name table" in entry["message"]
    ]

    assert len(unmapped_warnings) == 1
    assert "2 Literotica category id(s)" in unmapped_warnings[0]["message"]
    assert "999=brand-new" in unmapped_warnings[0]["message"]
    assert "1000=other-new" in unmapped_warnings[0]["message"]


def test_scan_does_not_warn_when_every_category_maps(monkeypatch) -> None:
    """scan emits no warning when all categories are mapped."""

    def fake_fetch(url: str) -> dict:
        return {
            "data": [
                {
                    "url": "a",
                    "category": 29,
                    "category_info": {"pageUrl": "mind-control"},
                },
                {
                    "url": "b",
                    "category": 29,
                    "category_info": {"pageUrl": "mind-control"},
                },
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=x"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    warn_logs = [entry for entry in logs if entry["level"] == "warning"]
    unmapped_warnings = [
        entry
        for entry in warn_logs
        if "Literotica category id(s) are not in this plugin's name table" in entry["message"]
    ]

    assert len(unmapped_warnings) == 0


def test_scan_does_not_warn_without_a_slug(monkeypatch) -> None:
    """scan does not warn when unmapped category has no slug to report."""

    def fake_fetch(url: str) -> dict:
        return {
            "data": [
                {
                    "url": "a",
                    "category": 999,
                },
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=x"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    warn_logs = [entry for entry in logs if entry["level"] == "warning"]
    unmapped_warnings = [
        entry
        for entry in warn_logs
        if "Literotica category id(s) are not in this plugin's name table" in entry["message"]
    ]

    assert len(unmapped_warnings) == 0


def test_scan_complete_line_is_still_last(monkeypatch) -> None:
    """scan keeps final 'Scan complete' line as the last info entry."""

    def fake_fetch(url: str) -> dict:
        return {
            "data": [
                {
                    "url": "a",
                    "category": 999,
                    "category_info": {"pageUrl": "brand-new"},
                },
            ]
        }

    monkeypatch.setattr(_MODULE, "fetch_json", fake_fetch)
    monkeypatch.setattr(_MODULE.time, "sleep", lambda _: None)

    settings = {
        "search_urls": ["https://search.literotica.com/?query=x"],
        "max_pages": 1,
    }
    stories, logs = _MODULE.scan(settings)

    # Last entry should be the Scan complete line
    assert logs[-1]["level"] == "info"
    assert "Scan complete:" in logs[-1]["message"]


def test_map_story_declares_metadata_fetched() -> None:
    """map_story declares metadata_fetched=True on every patch."""
    story_a = _FIXTURE_DATA["data"][0]
    result = _MODULE.map_story(story_a)
    assert result["metadata_fetched"] is True
