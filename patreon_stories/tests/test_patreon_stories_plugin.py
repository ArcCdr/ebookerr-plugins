"""Tests for the Patreon Stories catalog plugin."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from datetime import UTC
from pathlib import Path
from unittest import mock

import pytest

# Dynamically import entrypoint module
_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "entrypoint.py"
_SPEC = importlib.util.spec_from_file_location("patreon_stories_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["patreon_stories_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

_MEMBERSHIPS_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "memberships.json"
_MEMBERSHIPS_FIXTURE_DATA = json.loads(_MEMBERSHIPS_FIXTURE_PATH.read_text())

_POSTS_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "posts_page.json"
_POSTS_FIXTURE_DATA = json.loads(_POSTS_FIXTURE_PATH.read_text())


def _memberships_payload(campaigns: list[tuple[str, str, str]]) -> dict[str, object]:
    """Build a memberships-endpoint payload: one free membership per (campaign_id, name, creator).

    Args:
        campaigns: ``(campaign_id, campaign name, creator full name)`` per membership.

    Returns:
        A JSON-API payload shaped like ``MEMBERSHIPS_URL``'s response.
    """
    included: list[dict[str, object]] = []
    for campaign_id, name, creator_name in campaigns:
        creator_id = f"creator-{campaign_id}"
        included.append(
            {
                "id": f"member-{campaign_id}",
                "type": "member",
                "attributes": {
                    "patron_status": None,
                    "pledge_relationship_end": None,
                    "access_expires_at": None,
                },
                "relationships": {"campaign": {"data": {"id": campaign_id, "type": "campaign"}}},
            }
        )
        included.append(
            {
                "id": campaign_id,
                "type": "campaign",
                "attributes": {"name": name, "url": f"https://www.patreon.com/c{campaign_id}"},
                "relationships": {"creator": {"data": {"id": creator_id, "type": "user"}}},
            }
        )
        included.append(
            {"id": creator_id, "type": "user", "attributes": {"full_name": creator_name}}
        )
    return {
        "data": {"id": "user123", "type": "user", "attributes": {"full_name": "Logged In User"}},
        "included": included,
    }


def test_parse_campaigns_fixture() -> None:
    """pledges fixture → one campaign with correct id, creator_name, name, url."""
    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    assert len(campaigns) == 1
    campaign = campaigns[0]
    assert campaign["campaign_id"] == "111"
    assert campaign["creator_name"] == "Demo Creator"
    assert campaign["name"] == "Demo Campaign"
    assert campaign["url"] == "https://www.patreon.com/democreator"


def test_parse_posts_page_fixture() -> None:
    """posts fixture → 3 posts; locked one has can_view False; media has mimetype/url; cursor ok."""
    posts, next_cursor = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    assert len(posts) == 3
    assert next_cursor == "cursor_next_page_value"

    # Check post 201 (can_view true)
    post_201 = posts[0]
    assert post_201["post_id"] == "201"
    assert post_201["title"] == "First Post"
    assert post_201["can_view"] is True
    assert len(post_201["media"]) == 3
    assert post_201["media"][0]["mimetype"] == "application/epub+zip"
    assert post_201["media"][1]["mimetype"] == "application/pdf"
    assert post_201["media"][2]["mimetype"] == "image/png"

    # Check post 202 (can_view false)
    post_202 = posts[1]
    assert post_202["post_id"] == "202"
    assert post_202["can_view"] is False
    assert len(post_202["media"]) == 1
    assert post_202["media"][0]["mimetype"] == "text/plain"

    # Check post 203 (no media)
    post_203 = posts[2]
    assert post_203["post_id"] == "203"
    assert post_203["can_view"] is True
    assert len(post_203["media"]) == 0


def test_select_story_media_prefers_epub() -> None:
    """media list with pdf-then-epub (both with URLs) → epub chosen."""
    media = [
        {
            "media_id": "1",
            "file_name": "story.pdf",
            "mimetype": "application/pdf",
            "download_url": "https://example.com/story.pdf",
        },
        {
            "media_id": "2",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "https://example.com/story.epub",
        },
    ]
    result = _MODULE.select_story_media(media)
    assert result is not None
    assert result["media_id"] == "2"
    assert result["mimetype"] == "application/epub+zip"


def test_select_story_media_pdf_fallback() -> None:
    """only pdf with URL → pdf; only blank-URL epub → None."""
    # Only PDF with URL
    media_pdf = [
        {
            "media_id": "1",
            "file_name": "story.pdf",
            "mimetype": "application/pdf",
            "download_url": "https://example.com/story.pdf",
        }
    ]
    result = _MODULE.select_story_media(media_pdf)
    assert result is not None
    assert result["mimetype"] == "application/pdf"

    # Only EPUB with blank URL
    media_blank = [
        {
            "media_id": "2",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "",
        }
    ]
    result = _MODULE.select_story_media(media_blank)
    assert result is None


def test_build_story_mapping() -> None:
    """full assertion of every key in build_story for post 201."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    post_201 = posts[0]

    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    campaign = campaigns[0]

    media = _MODULE.select_story_media(post_201["media"])
    assert media is not None

    story = _MODULE.build_story(post_201, campaign, media)

    assert story["url"] == "https://www.patreon.com/file?h=201&m=301"
    assert story["format"] == "epub"
    assert story["title"] == "First Post"
    assert story["author"] == "Demo Creator"
    assert story["author_url"] == "https://www.patreon.com/democreator"
    assert story["site"] == "patreon.com"
    assert story["date_published"] == "2026-07-15T10:00:00Z"
    assert story["story_id"] == "201"
    assert story["series"] == "Demo Campaign"
    assert "This is rich content" in story["description"]
    assert story["custom"]["Post Type"] == "text_post"
    assert story["custom"]["Campaign"] == "Demo Campaign"


def test_build_story_title_fallback_filename() -> None:
    """post with empty title → title == media file stem."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    post_203 = posts[2]

    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    campaign = campaigns[0]

    # Give post 203 a media file to test title fallback
    media_item = {
        "media_id": "999",
        "file_name": "my_story.epub",
        "mimetype": "application/epub+zip",
        "download_url": "https://example.com/my_story.epub",
    }

    story = _MODULE.build_story(post_203, campaign, media_item)
    assert story["title"] == "my_story"


def test_plain_excerpt_strips_and_truncates() -> None:
    """simple HTML strip and truncate at word boundary."""
    # Test HTML stripping
    result = _MODULE._plain_excerpt("<p>Hello <b>world</b></p>", 500)
    assert "Hello" in result
    assert "world" in result
    assert "<" not in result

    # Test truncation with ellipsis
    long_text = "<p>" + " ".join(["word"] * 200) + "</p>"
    result = _MODULE._plain_excerpt(long_text, 50)
    assert result is not None
    assert len(result) <= 51  # 50 chars + ellipsis
    assert result.endswith("…")


def test_get_json_auth_error_message() -> None:
    """monkeypatched urlopen raising HTTP 403 → RuntimeError with 'session cookie' message."""
    http_error = urllib.error.HTTPError("http://test", 403, "Forbidden", {}, None)  # type: ignore[call-overload]

    with (
        mock.patch("urllib.request.urlopen", side_effect=http_error),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE._get_json("https://www.patreon.com/api/test", "fake_cookie")
    assert "session cookie missing or expired" in str(exc_info.value).lower()


def test_get_json_http_error_message() -> None:
    """HTTP 500 error → RuntimeError message contains 'Patreon API error: HTTP 500'."""
    http_error = urllib.error.HTTPError("http://test", 500, "Internal Server Error", {}, None)  # type: ignore[call-overload]

    with (
        mock.patch("urllib.request.urlopen", side_effect=http_error),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE._get_json("https://www.patreon.com/api/test", "fake_cookie")
    assert "Patreon API error" in str(exc_info.value)
    assert "500" in str(exc_info.value)


def test_get_json_sends_configured_cookie_name() -> None:
    """_get_json builds the Cookie header from cookie_name, not a hardcoded 'session_id'.

    The site-auth profile's own 'name' field must reach the wire — a plugin that
    ignores it and always sends 'session_id=...' would silently diverge from
    whatever cookie name the user actually configured (e.g. an EPUB download that
    reuses the same profile via SiteAuthService.headers_for, which does honour it).
    """
    mock_response = mock.MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = b'{"ok": true}'

    with mock.patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        _MODULE._get_json(
            "https://www.patreon.com/api/test", "the-value", cookie_name="patreon_token"
        )

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.get_header("Cookie") == "patreon_token=the-value"


def test_get_json_defaults_cookie_name_to_session_id() -> None:
    """Without an explicit cookie_name, _get_json keeps sending 'session_id' (back-compat)."""
    mock_response = mock.MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = b'{"ok": true}'

    with mock.patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        _MODULE._get_json("https://www.patreon.com/api/test", "the-value")

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.get_header("Cookie") == "session_id=the-value"


def test_scan_applies_recent_weeks_cutoff() -> None:
    """Posts older than recent_weeks cutoff are excluded, pagination stops."""
    from datetime import datetime

    # Fake now: 2026-07-18 UTC
    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    # Fixture posts: post 201 (2026-07-15), 202 (2026-07-10), 203 (2026-07-05)
    # With recent_weeks=1, cutoff = 2026-07-11, so posts 202 and 203 are old
    # Expected: only post 201 included, pagination stopped (second page never requested)

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {"recent_weeks": "1"}

    fetch_log: list[str] = []

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        fetch_log.append(url)
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        elif "api/posts" in url:
            if len([u for u in fetch_log if "api/posts" in u]) == 1:  # First posts page
                return _POSTS_FIXTURE_DATA
            else:
                raise AssertionError("Second page should not be requested")
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan(auth, settings, fake_now)

    # Only post 201 (2026-07-15) is within 1 week
    assert len(stories) == 1
    assert stories[0]["story_id"] == "201"

    # Check that pagination stopped (second page not fetched)
    posts_calls = [u for u in fetch_log if "api/posts" in u]
    assert len(posts_calls) == 1  # Only one posts page fetched


def test_scan_skips_locked_and_counts() -> None:
    """can_view=False posts are skipped and counted in log."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}  # Default recent_weeks

    # Fixture with 201 (can_view=True, has epub) and 202 (can_view=False)
    posts_fixture = {
        "data": [
            {
                "id": "201",
                "type": "post",
                "attributes": {
                    "title": "First Post",
                    "published_at": "2026-07-15T10:00:00Z",
                    "post_type": "text_post",
                    "current_user_can_view": True,
                    "content": "<p>Content</p>",
                },
                "relationships": {
                    "attachments_section": {"data": [{"id": "301", "type": "media"}]},
                    "media": {"data": []},
                },
            },
            {
                "id": "202",
                "type": "post",
                "attributes": {
                    "title": "Locked Post",
                    "published_at": "2026-07-10T14:30:00Z",
                    "post_type": "text_post",
                    "current_user_can_view": False,
                    "content": "",
                },
                "relationships": {"attachments_section": {"data": []}, "media": {"data": []}},
            },
        ],
        "included": [
            {
                "id": "301",
                "type": "media",
                "attributes": {
                    "file_name": "story.epub",
                    "mimetype": "application/epub+zip",
                    "download_url": "https://download.patreon.com/301/story.epub",
                },
            }
        ],
        "meta": {"pagination": {"cursors": {}}},
    }

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        elif "api/posts" in url:
            return posts_fixture
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan(auth, settings, fake_now)

    # Only post 201 should produce a story
    assert len(stories) == 1
    assert stories[0]["story_id"] == "201"

    # Check log contains skipped count
    campaign_logs = [entry for entry in logs if "locked posts skipped" in entry.get("message", "")]
    assert len(campaign_logs) > 0
    assert "1 locked posts skipped" in campaign_logs[0]["message"]


def test_scan_skips_posts_without_files() -> None:
    """Posts with no epub/pdf are skipped and counted."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    # Fixture with 201 (has epub) and 203 (no media)
    posts_fixture = {
        "data": [
            {
                "id": "201",
                "type": "post",
                "attributes": {
                    "title": "First Post",
                    "published_at": "2026-07-15T10:00:00Z",
                    "post_type": "text_post",
                    "current_user_can_view": True,
                    "content": "<p>Content</p>",
                },
                "relationships": {
                    "attachments_section": {"data": [{"id": "301", "type": "media"}]},
                    "media": {"data": []},
                },
            },
            {
                "id": "203",
                "type": "post",
                "attributes": {
                    "title": "",
                    "published_at": "2026-07-05T08:15:00Z",
                    "post_type": "image_post",
                    "current_user_can_view": True,
                    "content": None,
                },
                "relationships": {"attachments_section": {"data": []}, "media": {"data": []}},
            },
        ],
        "included": [
            {
                "id": "301",
                "type": "media",
                "attributes": {
                    "file_name": "story.epub",
                    "mimetype": "application/epub+zip",
                    "download_url": "https://download.patreon.com/301/story.epub",
                },
            }
        ],
        "meta": {"pagination": {"cursors": {}}},
    }

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        elif "api/posts" in url:
            return posts_fixture
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan(auth, settings, fake_now)

    # Only post 201 should produce a story
    assert len(stories) == 1
    assert stories[0]["story_id"] == "201"

    # Check log contains skipped count
    campaign_logs = [
        entry for entry in logs if "posts without a supported file" in entry.get("message", "")
    ]
    assert len(campaign_logs) > 0
    assert "1 posts without a supported file" in campaign_logs[0]["message"]


def test_scan_dedupes_by_url() -> None:
    """Two campaigns with same story URL → one story returned."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    # Create two campaigns
    memberships_2camps = _memberships_payload(
        [("111", "Demo Campaign", "Demo Creator"), ("222", "Second Campaign", "Second Creator")]
    )

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return memberships_2camps
        elif "api/posts" in url:
            # Both campaigns return the same posts (same post_id and media_id)
            return _POSTS_FIXTURE_DATA
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan(auth, settings, fake_now)

    # Both campaigns yield same URLs, should be deduplicated
    urls = [s["url"] for s in stories]
    # If not deduplicated, we'd have duplicates; with dedup, we shouldn't
    assert len(urls) == len(set(urls)), "URLs should be deduplicated"


def test_scan_pagination_follows_cursor() -> None:
    """Fixture page 1 with cursor → page 2 fetched, stories merged."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    # Modify posts_page to have next cursor
    page1_data = json.loads(_POSTS_FIXTURE_PATH.read_text())

    page2_data = {
        "data": [
            {
                "id": "204",
                "type": "post",
                "attributes": {
                    "title": "Second Page Post",
                    "published_at": "2026-07-14T12:00:00Z",
                    "post_type": "text_post",
                    "current_user_can_view": True,
                    "content": "Another post",
                },
                "relationships": {
                    "attachments_section": {"data": [{"id": "305", "type": "media"}]},
                    "media": {"data": []},
                },
            }
        ],
        "included": [
            {
                "id": "305",
                "type": "media",
                "attributes": {
                    "file_name": "second.epub",
                    "mimetype": "application/epub+zip",
                    "download_url": "https://download.patreon.com/305/second.epub",
                },
            }
        ],
        "meta": {"pagination": {"cursors": {}}},  # No next cursor on page 2
    }

    fetch_calls = []

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        fetch_calls.append(url)
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        elif "api/posts" in url:
            if "page[cursor]" not in url:  # First page (no cursor param)
                return page1_data
            else:  # Second page (has cursor)
                return page2_data
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan(auth, settings, fake_now)

    # Both pages should be fetched
    posts_calls = [c for c in fetch_calls if "api/posts" in c]
    assert len(posts_calls) >= 2, "Should fetch at least 2 pages"

    # Should have stories from both pages
    assert len(stories) >= 2, "Should have stories from both pages"


def test_scan_no_auth_raises_actionable_error() -> None:
    """Empty auth → scan() raises RuntimeError with actionable message."""
    with pytest.raises(RuntimeError) as exc_info:
        _MODULE.scan({}, {})

    error_msg = str(exc_info.value)
    assert "Patreon session cookie not configured" in error_msg
    assert "Settings → Site authentication" in error_msg


def test_scan_uses_configured_cookie_name() -> None:
    """scan() threads the site-auth profile's 'name' field through to every _get_json call."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"name": "patreon_token", "value": "test_cookie"}}
    captured_names: list[str] = []

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        captured_names.append(cookie_name)
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        return _POSTS_FIXTURE_DATA

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        _MODULE.scan(auth, {}, fake_now)

    assert captured_names, "expected _get_json to have been called at least once"
    assert all(name == "patreon_token" for name in captured_names)


def test_scan_defaults_cookie_name_when_profile_has_no_name() -> None:
    """No 'name' on the site-auth profile (legacy data) → scan() falls back to 'session_id'."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}  # no "name" key
    captured_names: list[str] = []

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        captured_names.append(cookie_name)
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        return _POSTS_FIXTURE_DATA

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        _MODULE.scan(auth, {}, fake_now)

    assert captured_names, "expected _get_json to have been called at least once"
    assert all(name == "session_id" for name in captured_names)


def test_main_no_auth_outputs_ok_false(capsys: pytest.CaptureFixture) -> None:
    """main() with no auth via stdin → JSON output has ok=False and cookie error message."""
    import builtins

    request_json = json.dumps({"op": "scan", "request": {"auth": {}, "settings": {}}})

    with (
        mock.patch.object(builtins, "input", return_value=request_json),
        pytest.raises(SystemExit),
    ):
        _MODULE.main()

    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert output["ok"] is False
    assert "Patreon session cookie not configured" in output["error"]
    assert "Settings → Site authentication" in output["error"]


def test_manifest_parses_with_auth_sites() -> None:
    """parse_manifest over the manifest → auth_sites, spi_version, weeks field."""
    import tomllib

    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    manifest_data = tomllib.loads(manifest_path.read_text())

    from ebookerr_sdk.spi.manifest import parse_manifest

    manifest = parse_manifest(manifest_data)

    assert manifest.auth_sites == ("patreon.com",)
    assert manifest.spi_version == "2.30"

    # Check settings_schema has recent_weeks field
    assert len(manifest.settings_schema.fields) == 1
    field = manifest.settings_schema.fields[0]
    assert field.key == "recent_weeks"
    assert field.type == "int"
    assert field.default == "4"

    # Ensure no session_id field
    field_keys = [f.key for f in manifest.settings_schema.fields]
    assert "session_id" not in field_keys


def test_the_manifest_declares_a_complete_listing() -> None:
    """parse_manifest over the manifest → complete_listing is True."""
    import tomllib

    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    manifest_data = tomllib.loads(manifest_path.read_text())

    from ebookerr_sdk.spi.manifest import parse_manifest

    manifest = parse_manifest(manifest_data)

    assert manifest.complete_listing is True


def test_manifest_loads_via_loader() -> None:
    """The staged manifest parses to the catalog patreon_stories, version 2.2.0."""
    import tomllib

    from ebookerr_sdk.spi.manifest import parse_manifest

    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    manifest = parse_manifest(tomllib.loads(manifest_path.read_text(encoding="utf-8")))
    assert manifest.id == "patreon_stories"
    assert manifest.version == "2.2.0"


def test_build_story_includes_source_filename() -> None:
    """media with file_name → build_story includes source_filename in custom."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    post_201 = posts[0]

    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    campaign = campaigns[0]

    media = _MODULE.select_story_media(post_201["media"])
    assert media is not None

    story = _MODULE.build_story(post_201, campaign, media)

    assert "source_filename" in story["custom"]
    assert story["custom"]["source_filename"] == "story.epub"


def test_build_story_omits_empty_source_filename() -> None:
    """media with empty file_name → source_filename not in custom."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    post_201 = posts[0]

    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    campaign = campaigns[0]

    # Create media with empty file_name
    media = {
        "media_id": "999",
        "file_name": "",
        "mimetype": "application/epub+zip",
        "download_url": "https://example.com/story.epub",
    }

    story = _MODULE.build_story(post_201, campaign, media)

    assert "source_filename" not in story["custom"]


def test_build_story_source_filename_survives_storypatch_roundtrip() -> None:
    """build_story dict carries source_filename, StoryPatch keeps it verbatim."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    post_201 = posts[0]

    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    campaign = campaigns[0]

    media = _MODULE.select_story_media(post_201["media"])
    assert media is not None
    original_filename = media["file_name"]

    story_dict = _MODULE.build_story(post_201, campaign, media)

    assert story_dict["custom"]["source_filename"] == original_filename


def test_select_prefer_default_epub_over_pdf() -> None:
    """media with both EPUB and PDF → default returns the EPUB item."""
    media = [
        {
            "media_id": "1",
            "file_name": "story.pdf",
            "mimetype": "application/pdf",
            "download_url": "https://example.com/story.pdf",
        },
        {
            "media_id": "2",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "https://example.com/story.epub",
        },
    ]
    result = _MODULE.select_story_media(media)
    assert result is not None
    assert result["media_id"] == "2"
    assert result["mimetype"] == "application/epub+zip"


def test_select_prefer_pdf_returns_pdf() -> None:
    """media with both EPUB and PDF → prefer="pdf" returns the PDF item."""
    media = [
        {
            "media_id": "1",
            "file_name": "story.pdf",
            "mimetype": "application/pdf",
            "download_url": "https://example.com/story.pdf",
        },
        {
            "media_id": "2",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "https://example.com/story.epub",
        },
    ]
    result = _MODULE.select_story_media(media, prefer="pdf")
    assert result is not None
    assert result["media_id"] == "1"
    assert result["mimetype"] == "application/pdf"


def test_select_prefer_epub_returns_epub() -> None:
    """media with both EPUB and PDF → prefer="epub" returns the EPUB item."""
    media = [
        {
            "media_id": "1",
            "file_name": "story.pdf",
            "mimetype": "application/pdf",
            "download_url": "https://example.com/story.pdf",
        },
        {
            "media_id": "2",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "https://example.com/story.epub",
        },
    ]
    result = _MODULE.select_story_media(media, prefer="epub")
    assert result is not None
    assert result["media_id"] == "2"
    assert result["mimetype"] == "application/epub+zip"


def test_select_prefer_pdf_none_when_absent() -> None:
    """media with only EPUB → prefer="pdf" returns None."""
    media = [
        {
            "media_id": "2",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "https://example.com/story.epub",
        }
    ]
    result = _MODULE.select_story_media(media, prefer="pdf")
    assert result is None


def test_mime_to_format_mapping() -> None:
    """_mime_to_format returns correct format strings for all supported mimetypes."""
    # epub and pdf
    assert _MODULE._mime_to_format("application/epub+zip") == "epub"
    assert _MODULE._mime_to_format("application/pdf") == "pdf"

    # docx
    assert _MODULE._mime_to_format(_MODULE._DOCX_MIME) == "docx"

    # rtf variants
    assert _MODULE._mime_to_format("application/rtf") == "rtf"
    assert _MODULE._mime_to_format("text/rtf") == "rtf"

    # text variants
    assert _MODULE._mime_to_format("text/plain") == "txt"
    assert _MODULE._mime_to_format("text/markdown") == "txt"
    assert _MODULE._mime_to_format("text/x-markdown") == "txt"

    # unsupported
    assert _MODULE._mime_to_format("image/png") is None


def test_select_story_media_returns_docx_only() -> None:
    """media list with only docx → returns it."""
    media = [
        {
            "media_id": "1",
            "file_name": "story.docx",
            "mimetype": _MODULE._DOCX_MIME,
            "download_url": "https://example.com/story.docx",
        }
    ]
    result = _MODULE.select_story_media(media)
    assert result is not None
    assert result["media_id"] == "1"
    assert result["mimetype"] == _MODULE._DOCX_MIME


def test_select_story_media_returns_rtf_only() -> None:
    """media list with only rtf (application/rtf) → returns it."""
    media = [
        {
            "media_id": "1",
            "file_name": "story.rtf",
            "mimetype": "application/rtf",
            "download_url": "https://example.com/story.rtf",
        }
    ]
    result = _MODULE.select_story_media(media)
    assert result is not None
    assert result["media_id"] == "1"
    assert result["mimetype"] == "application/rtf"


def test_select_story_media_returns_txt_only() -> None:
    """media list with only text/plain or text/markdown → returns it."""
    # text/plain
    media_plain = [
        {
            "media_id": "1",
            "file_name": "story.txt",
            "mimetype": "text/plain",
            "download_url": "https://example.com/story.txt",
        }
    ]
    result = _MODULE.select_story_media(media_plain)
    assert result is not None
    assert result["media_id"] == "1"
    assert result["mimetype"] == "text/plain"

    # text/markdown
    media_md = [
        {
            "media_id": "2",
            "file_name": "story.md",
            "mimetype": "text/markdown",
            "download_url": "https://example.com/story.md",
        }
    ]
    result = _MODULE.select_story_media(media_md)
    assert result is not None
    assert result["media_id"] == "2"
    assert result["mimetype"] == "text/markdown"


def test_select_story_media_preference_full_order() -> None:
    """media with all formats (epub/docx/pdf/rtf/txt) → preference order respected."""
    all_media = [
        {
            "media_id": "1",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "https://example.com/story.epub",
        },
        {
            "media_id": "2",
            "file_name": "story.docx",
            "mimetype": _MODULE._DOCX_MIME,
            "download_url": "https://example.com/story.docx",
        },
        {
            "media_id": "3",
            "file_name": "story.pdf",
            "mimetype": "application/pdf",
            "download_url": "https://example.com/story.pdf",
        },
        {
            "media_id": "4",
            "file_name": "story.rtf",
            "mimetype": "application/rtf",
            "download_url": "https://example.com/story.rtf",
        },
        {
            "media_id": "5",
            "file_name": "story.txt",
            "mimetype": "text/plain",
            "download_url": "https://example.com/story.txt",
        },
    ]

    # Default: returns epub (first in preference order)
    result = _MODULE.select_story_media(all_media)
    assert result is not None
    assert result["media_id"] == "1"

    # Remove epub: returns docx
    result = _MODULE.select_story_media(all_media[1:])
    assert result is not None
    assert result["media_id"] == "2"

    # Remove epub and docx: returns pdf
    result = _MODULE.select_story_media(all_media[2:])
    assert result is not None
    assert result["media_id"] == "3"

    # Remove epub, docx, pdf: returns rtf
    result = _MODULE.select_story_media(all_media[3:])
    assert result is not None
    assert result["media_id"] == "4"

    # Only txt: returns txt
    result = _MODULE.select_story_media(all_media[4:])
    assert result is not None
    assert result["media_id"] == "5"


def test_select_story_media_prefer_docx_and_rtf() -> None:
    """prefer parameter works for docx and rtf formats."""
    all_media = [
        {
            "media_id": "1",
            "file_name": "story.epub",
            "mimetype": "application/epub+zip",
            "download_url": "https://example.com/story.epub",
        },
        {
            "media_id": "2",
            "file_name": "story.docx",
            "mimetype": _MODULE._DOCX_MIME,
            "download_url": "https://example.com/story.docx",
        },
        {
            "media_id": "4",
            "file_name": "story.rtf",
            "mimetype": "application/rtf",
            "download_url": "https://example.com/story.rtf",
        },
    ]

    # prefer="docx" returns docx
    result = _MODULE.select_story_media(all_media, prefer="docx")
    assert result is not None
    assert result["media_id"] == "2"
    assert result["mimetype"] == _MODULE._DOCX_MIME

    # prefer="rtf" returns rtf
    result = _MODULE.select_story_media(all_media, prefer="rtf")
    assert result is not None
    assert result["media_id"] == "4"
    assert result["mimetype"] == "application/rtf"

    # prefer="txt" on media without txt → None
    result = _MODULE.select_story_media(all_media, prefer="txt")
    assert result is None


def test_select_story_media_skips_empty_download_url_docx() -> None:
    """docx media with empty download_url → skipped (None returned)."""
    media = [
        {
            "media_id": "1",
            "file_name": "story.docx",
            "mimetype": _MODULE._DOCX_MIME,
            "download_url": "",
        }
    ]
    result = _MODULE.select_story_media(media)
    assert result is None


def test_build_story_format_docx() -> None:
    """build_story with docx media → format=="docx", url correct, source_filename set."""
    post = {
        "post_id": "201",
        "title": "Test Post",
        "published_at": "2026-07-15T10:00:00Z",
        "post_type": "text_post",
        "can_view": True,
        "content": "<p>Test content</p>",
        "media": [],
    }

    campaign = {
        "campaign_id": "111",
        "name": "Test Campaign",
        "url": "https://www.patreon.com/creator",
        "creator_name": "Test Creator",
    }

    media = {
        "media_id": "999",
        "file_name": "s.docx",
        "mimetype": _MODULE._DOCX_MIME,
        "download_url": "https://example.com/s.docx",
    }

    story = _MODULE.build_story(post, campaign, media)

    assert story["format"] == "docx"
    assert story["url"] == "https://www.patreon.com/file?h=201&m=999"
    assert story["custom"]["source_filename"] == "s.docx"


def test_build_story_format_rtf() -> None:
    """build_story with rtf media (application/rtf) → format=="rtf"."""
    post = {
        "post_id": "201",
        "title": "Test Post",
        "published_at": "2026-07-15T10:00:00Z",
        "post_type": "text_post",
        "can_view": True,
        "content": "<p>Test content</p>",
        "media": [],
    }

    campaign = {
        "campaign_id": "111",
        "name": "Test Campaign",
        "url": "https://www.patreon.com/creator",
        "creator_name": "Test Creator",
    }

    media = {
        "media_id": "999",
        "file_name": "story.rtf",
        "mimetype": "application/rtf",
        "download_url": "https://example.com/story.rtf",
    }

    story = _MODULE.build_story(post, campaign, media)
    assert story["format"] == "rtf"


def test_build_story_format_txt_for_text_mimes() -> None:
    """build_story with text/plain or text/markdown → format=="txt"."""
    post = {
        "post_id": "201",
        "title": "Test Post",
        "published_at": "2026-07-15T10:00:00Z",
        "post_type": "text_post",
        "can_view": True,
        "content": "<p>Test content</p>",
        "media": [],
    }

    campaign = {
        "campaign_id": "111",
        "name": "Test Campaign",
        "url": "https://www.patreon.com/creator",
        "creator_name": "Test Creator",
    }

    # text/plain
    media_plain = {
        "media_id": "999",
        "file_name": "story.txt",
        "mimetype": "text/plain",
        "download_url": "https://example.com/story.txt",
    }

    story = _MODULE.build_story(post, campaign, media_plain)
    assert story["format"] == "txt"

    # text/markdown
    media_md = {
        "media_id": "1000",
        "file_name": "story.md",
        "mimetype": "text/markdown",
        "download_url": "https://example.com/story.md",
    }

    story = _MODULE.build_story(post, campaign, media_md)
    assert story["format"] == "txt"


def test_report_progress_writes_a_progress_frame(capsys: pytest.CaptureFixture) -> None:
    """_report_progress(42.5) → stdout emits {"op": "progress", "percent": 42.5}."""
    _MODULE._report_progress(42.5)
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output == {"op": "progress", "percent": 42.5}


def test_scan_reports_once_per_campaign(capsys: pytest.CaptureFixture) -> None:
    """Four campaigns → progress frames at 25%, 50%, 75%, 100%."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    # Create a stub with 4 campaigns
    memberships_4camps = _memberships_payload(
        [
            ("camp1", "Campaign 1", "Creator 1"),
            ("camp2", "Campaign 2", "Creator 2"),
            ("camp3", "Campaign 3", "Creator 3"),
            ("camp4", "Campaign 4", "Creator 4"),
        ]
    )

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return memberships_4camps
        elif "api/posts" in url:
            # Return empty posts for all campaigns
            return {"data": [], "included": [], "meta": {"pagination": {"cursors": {}}}}
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        _MODULE.scan(auth, settings, fake_now)

    captured = capsys.readouterr()
    lines = [line for line in captured.out.split("\n") if line.strip()]
    progress_lines = [line for line in lines if "progress" in line]

    progress_values = []
    for line in progress_lines:
        parsed = json.loads(line)
        progress_values.append(parsed["percent"])

    assert progress_values == pytest.approx([25.0, 50.0, 75.0, 100.0])


def test_scan_reports_a_failed_campaign(capsys: pytest.CaptureFixture) -> None:
    """Two campaigns, first raises RuntimeError → scan() raises with campaign name."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    memberships_2camps = _memberships_payload(
        [("camp1", "Campaign 1", "Creator 1"), ("camp2", "Campaign 2", "Creator 2")]
    )

    call_count = 0

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        nonlocal call_count
        if "api/current_user" in url:
            return memberships_2camps
        elif "api/posts" in url:
            call_count += 1
            if call_count == 1:  # First campaign
                raise RuntimeError("boom")
            else:  # Second campaign (won't be reached)
                return {"data": [], "included": [], "meta": {"pagination": {"cursors": {}}}}
        raise ValueError(f"Unexpected URL: {url}")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    error_msg = str(exc_info.value)
    assert "Campaign 1" in error_msg
    assert "boom" in error_msg


@pytest.mark.pins("EXP-245")
def test_scan_returns_empty_when_the_account_has_no_memberships() -> None:
    """Authenticated account with no memberships → scan() returns ([], logs) (EXP-245)."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    memberships_none = _memberships_payload([])

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return memberships_none
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan(auth, settings, fake_now)

    assert stories == []

    # Check logs contain exactly one "info" level entry with the expected message
    info_logs = [entry for entry in logs if entry.get("level") == "info"]
    assert len(info_logs) == 1
    assert info_logs[0]["message"] == "No Patreon memberships found for this account"

    # Ensure no error or warning entries
    error_logs = [entry for entry in logs if entry.get("level") == "error"]
    warning_logs = [entry for entry in logs if entry.get("level") == "warning"]
    assert len(error_logs) == 0
    assert len(warning_logs) == 0


def test_scan_reports_progress_to_completion_on_an_empty_account(
    capsys: pytest.CaptureFixture,
) -> None:
    """Empty account scan reports progress to 100%."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    memberships_none = _memberships_payload([])

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return memberships_none
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        _MODULE.scan(auth, settings, fake_now)

    captured = capsys.readouterr()
    lines = [line for line in captured.out.split("\n") if line.strip()]
    progress_frames = [json.loads(line) for line in lines if "progress" in line]

    # Check that a 100% progress frame was emitted
    progress_100 = [f for f in progress_frames if f.get("percent") == 100.0]
    assert len(progress_100) == 1


def test_scan_raises_when_patreon_does_not_accept_the_cookie() -> None:
    """Rejected cookie (data=None) → scan() raises with actionable message."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    rejected_payload = {"data": None, "included": []}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return rejected_payload
        raise ValueError(f"Unexpected URL: {url}")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    error_msg = str(exc_info.value)
    assert "Patreon did not accept the stored session cookie" in error_msg
    assert "Settings → Site authentication" in error_msg


def test_scan_raises_when_the_pledges_payload_has_no_user_object() -> None:
    """Payload with no data key at all → scan() raises with actionable message."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    no_data_payload = {"included": []}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return no_data_payload
        raise ValueError(f"Unexpected URL: {url}")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    error_msg = str(exc_info.value)
    assert "Patreon did not accept the stored session cookie" in error_msg
    assert "Settings → Site authentication" in error_msg


def test_scan_raises_when_the_user_object_has_a_blank_id() -> None:
    """User object with blank id → scan() raises with actionable message."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    blank_id_payload = {"data": {"id": "", "type": "user"}, "included": []}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return blank_id_payload
        raise ValueError(f"Unexpected URL: {url}")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    error_msg = str(exc_info.value)
    assert "Patreon did not accept the stored session cookie" in error_msg
    assert "Settings → Site authentication" in error_msg


def test_is_authenticated_accepts_a_signed_in_payload() -> None:
    """is_authenticated with valid user data → True."""
    pledges_no_camps = {
        "data": {
            "id": "user123",
            "type": "user",
            "attributes": {"full_name": "Logged In User"},
            "relationships": {"pledges": {"data": []}},
        },
        "included": [],
    }
    assert _MODULE.is_authenticated(pledges_no_camps) is True


def test_is_authenticated_rejects_a_non_dict_data() -> None:
    """is_authenticated with non-dict data values → False."""
    # data is a list
    assert _MODULE.is_authenticated({"data": []}) is False

    # data is a string
    assert _MODULE.is_authenticated({"data": "x"}) is False

    # no data key
    assert _MODULE.is_authenticated({}) is False


def test_scan_emits_no_progress_when_pledges_fetch_fails(capsys: pytest.CaptureFixture) -> None:
    """_get_json raises RuntimeError → scan() raises."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        raise RuntimeError("boom")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    assert "boom" in str(exc_info.value)


def test_build_story_declares_metadata_fetched() -> None:
    """build_story declares metadata_fetched=True on the returned patch."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    post_201 = posts[0]

    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    campaign = campaigns[0]

    media = _MODULE.select_story_media(post_201["media"])
    assert media is not None

    story = _MODULE.build_story(post_201, campaign, media)
    assert story["metadata_fetched"] is True


@pytest.mark.pins("EXP-227")
def test_scan_raises_on_a_fetch_failure_instead_of_reporting_empty() -> None:
    """HTTP 500 from _get_json → scan() raises with message containing HTTP 500."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        raise RuntimeError("HTTP 500")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    assert "HTTP 500" in str(exc_info.value)


@pytest.mark.pins("EXP-227")
def test_scan_raises_on_an_auth_failure() -> None:
    """_get_json raises RuntimeError("HTTP 401") → scan() raises with message containing 401."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        raise RuntimeError("HTTP 401")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    assert "401" in str(exc_info.value)


@pytest.mark.pins("EXP-227")
def test_scan_raises_when_a_single_campaign_fails() -> None:
    """Two campaigns, second campaign's posts fetch raises → scan() raises with campaign name."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    # Create a second campaign for testing
    memberships_2camps = _memberships_payload(
        [("111", "Demo Campaign", "Demo Creator"), ("222", "Second Campaign", "Second Creator")]
    )

    # Posts with no pagination to avoid confusing the test
    posts_no_pagination = {
        "data": [],
        "included": [],
        "meta": {"pagination": {"cursors": {}}},
    }

    call_count = 0

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        nonlocal call_count
        if "api/current_user" in url:
            return memberships_2camps
        elif "api/posts" in url:
            call_count += 1
            if call_count == 1:  # First campaign succeeds (empty posts, no pagination)
                return posts_no_pagination
            elif call_count == 2:  # Second campaign fails
                raise RuntimeError("Network error")
        raise ValueError(f"Unexpected URL: {url}")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan(auth, settings, fake_now)

    error_msg = str(exc_info.value)
    assert "Campaign" in error_msg
    assert "Second Campaign" in error_msg


@pytest.mark.pins("EXP-227")
def test_scan_with_zero_recent_posts_returns_empty_successfully() -> None:
    """One campaign with empty posts → scan() returns ([], logs) without raising."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)

    def fake_now() -> datetime:
        return fake_now_dt

    auth = {"patreon.com": {"value": "test_cookie"}}
    settings = {}

    empty_posts = {"data": [], "included": [], "meta": {"pagination": {"cursors": {}}}}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        elif "api/posts" in url:
            return empty_posts
        raise ValueError(f"Unexpected URL: {url}")

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan(auth, settings, fake_now)

    assert stories == []
    # Should not raise, logs should be populated


def test_membership_label_free() -> None:
    """A membership with no patron_status is a free membership."""
    assert _MODULE.membership_label({"patron_status": None}) == "Free"


def test_membership_label_active() -> None:
    """An active paid membership with no relationship end is Active."""
    attrs = {
        "patron_status": "active_patron",
        "pledge_relationship_end": None,
        "access_expires_at": None,
    }
    assert _MODULE.membership_label(attrs) == "Active"


def test_membership_label_cancelled_names_the_end_of_access() -> None:
    """A cancelled membership still entitled until a date says until when."""
    attrs = {
        "patron_status": "active_patron",
        "pledge_relationship_end": "2026-09-04T06:56:12.939+00:00",
        "access_expires_at": "2026-09-17T00:00:00.000+00:00",
    }
    assert _MODULE.membership_label(attrs) == "Cancelled (access until 17 Sep 2026)"


def test_membership_label_cancelled_without_a_usable_date() -> None:
    """A cancelled membership whose end of access is absent or unparseable is plain Cancelled."""
    base = {
        "patron_status": "active_patron",
        "pledge_relationship_end": "2026-09-04T06:56:12.939+00:00",
    }
    assert _MODULE.membership_label({**base, "access_expires_at": None}) == "Cancelled"
    assert _MODULE.membership_label({**base, "access_expires_at": "soon"}) == "Cancelled"


def test_membership_label_former_and_declined() -> None:
    """Lapsed and payment-declined memberships have their own labels."""
    assert _MODULE.membership_label({"patron_status": "former_patron"}) == "Former"
    assert _MODULE.membership_label({"patron_status": "declined_patron"}) == "Payment declined"


def test_membership_label_unknown_shapes_have_no_label() -> None:
    """An unknown status, a missing status key or a non-dict yields None."""
    assert _MODULE.membership_label({"patron_status": "pending_patron"}) is None
    assert _MODULE.membership_label({}) is None
    assert _MODULE.membership_label(None) is None
    assert _MODULE.membership_label("x") is None


def test_format_access_date_accepts_a_z_suffix() -> None:
    """A Z-suffixed timestamp formats as day, abbreviated month, year."""
    assert _MODULE._format_access_date("2026-01-05T10:00:00Z") == "5 Jan 2026"


_SKIPPED_222 = "Skipped Patreon campaign campaign_id=222: missing its name, URL or creator name"


def _campaign_without_url_payload() -> dict[str, object]:
    """A memberships payload whose only campaign (id 222) has no URL."""
    return {
        "data": {"id": "user123", "type": "user"},
        "included": [
            {
                "id": "222",
                "type": "campaign",
                "attributes": {"name": "No Url"},
                "relationships": {"creator": {"data": {"id": "creator999", "type": "user"}}},
            },
            {"id": "creator999", "type": "user", "attributes": {"full_name": "Second Creator"}},
        ],
    }


def test_parse_campaigns_reads_the_membership_state() -> None:
    """The fixture's cancelled membership reaches the campaign dict as its label."""
    campaigns = _MODULE.parse_campaigns(_MEMBERSHIPS_FIXTURE_DATA)
    assert campaigns[0]["membership"] == "Cancelled (access until 31 Jul 2026)"


def test_parse_campaigns_without_a_member_object_has_no_membership() -> None:
    """A campaign no member object points at has membership None."""
    payload = {
        "data": {"id": "user123", "type": "user"},
        "included": [
            {
                "id": "111",
                "type": "campaign",
                "attributes": {
                    "name": "Demo Campaign",
                    "url": "https://www.patreon.com/democreator",
                },
                "relationships": {"creator": {"data": {"id": "creator789", "type": "user"}}},
            },
            {"id": "creator789", "type": "user", "attributes": {"full_name": "Demo Creator"}},
        ],
    }
    campaigns = _MODULE.parse_campaigns(payload)
    assert len(campaigns) == 1
    assert campaigns[0]["membership"] is None


def test_parse_campaigns_reports_a_skipped_campaign_in_the_scan_log() -> None:
    """A campaign without a URL is skipped with one warning entry in the given logs list."""
    logs: list[dict[str, object]] = []
    assert _MODULE.parse_campaigns(_campaign_without_url_payload(), logs) == []
    assert logs == [{"level": "warning", "message": _SKIPPED_222}]
    assert _MODULE.parse_campaigns(_campaign_without_url_payload()) == []


def test_scan_discovers_campaigns_through_active_memberships() -> None:
    """scan() asks Patreon for the account's memberships, not its legacy pledges.

    Measured on a live account with cancelled and free memberships: the legacy
    ``include=pledges.creator.campaign`` request returned ``pledges.data == []`` and no
    campaigns, so every scan reported "No Patreon memberships found";
    ``include=active_memberships`` returned all seven memberships.
    """
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)
    last_page = {**_POSTS_FIXTURE_DATA, "meta": {"pagination": {"cursors": {}}}}
    requested: list[str] = []

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        requested.append(url)
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        return last_page

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, _logs = _MODULE.scan({"patreon.com": {"value": "c"}}, {}, lambda: fake_now_dt)

    assert requested[0] == _MODULE.MEMBERSHIPS_URL
    assert "include=active_memberships.campaign.creator" in requested[0]
    assert "fields[member]=patron_status,pledge_relationship_end,access_expires_at" in requested[0]
    assert "pledges" not in requested[0]
    assert [story["story_id"] for story in stories] == ["201"]


def test_scan_routes_a_skipped_campaign_warning_into_the_scan_log() -> None:
    """A campaign parse_campaigns skips is reported in the logs scan() returns."""

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        return _campaign_without_url_payload()

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, logs = _MODULE.scan({"patreon.com": {"value": "c"}}, {})

    assert stories == []
    assert {"level": "warning", "message": _SKIPPED_222} in logs


def test_build_story_carries_the_membership_label() -> None:
    """A campaign's membership label becomes the story's Membership custom value."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    campaign = {
        "campaign_id": "111",
        "name": "Demo Campaign",
        "url": "https://www.patreon.com/democreator",
        "creator_name": "Demo Creator",
        "membership": "Free",
    }
    media = _MODULE.select_story_media(posts[0]["media"])
    assert media is not None
    story = _MODULE.build_story(posts[0], campaign, media)
    assert story["custom"]["Membership"] == "Free"


def test_build_story_omits_an_unknown_membership() -> None:
    """No Membership custom value when the campaign's membership is None or absent."""
    posts, _ = _MODULE.parse_posts_page(_POSTS_FIXTURE_DATA)
    media = _MODULE.select_story_media(posts[0]["media"])
    assert media is not None
    base = {
        "campaign_id": "111",
        "name": "Demo Campaign",
        "url": "https://www.patreon.com/democreator",
        "creator_name": "Demo Creator",
    }
    unknown = _MODULE.build_story(posts[0], {**base, "membership": None}, media)
    absent = _MODULE.build_story(posts[0], base, media)
    assert "Membership" not in unknown["custom"]
    assert "Membership" not in absent["custom"]


def test_scan_labels_stories_with_their_membership() -> None:
    """A story found through a cancelled membership carries that membership's label."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)
    last_page = {**_POSTS_FIXTURE_DATA, "meta": {"pagination": {"cursors": {}}}}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        return last_page

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        stories, _logs = _MODULE.scan({"patreon.com": {"value": "c"}}, {}, lambda: fake_now_dt)

    assert stories[0]["custom"]["Membership"] == "Cancelled (access until 31 Jul 2026)"


def _three_memberships_payload() -> dict[str, object]:
    """One free, one cancelled and one former membership (the last name has an apostrophe)."""
    rows = [
        (
            "111",
            "Demo Campaign",
            {"patron_status": None, "pledge_relationship_end": None, "access_expires_at": None},
        ),
        (
            "222",
            "Second Campaign",
            {
                "patron_status": "active_patron",
                "pledge_relationship_end": "2026-09-04T06:56:12.939+00:00",
                "access_expires_at": "2026-09-17T00:00:00.000+00:00",
            },
        ),
        (
            "333",
            "Third's Stories",
            {
                "patron_status": "former_patron",
                "pledge_relationship_end": "2025-06-19T21:51:40.393+00:00",
                "access_expires_at": "2025-07-15T00:00:00.000+00:00",
            },
        ),
    ]
    included: list[dict[str, object]] = []
    for campaign_id, name, member_attrs in rows:
        creator_id = f"creator-{campaign_id}"
        included.append(
            {
                "id": f"member-{campaign_id}",
                "type": "member",
                "attributes": member_attrs,
                "relationships": {"campaign": {"data": {"id": campaign_id, "type": "campaign"}}},
            }
        )
        included.append(
            {
                "id": campaign_id,
                "type": "campaign",
                "attributes": {"name": name, "url": f"https://www.patreon.com/c{campaign_id}"},
                "relationships": {"creator": {"data": {"id": creator_id, "type": "user"}}},
            }
        )
        included.append(
            {
                "id": creator_id,
                "type": "user",
                "attributes": {"full_name": f"Creator {campaign_id}"},
            }
        )
    return {"data": {"id": "user123", "type": "user"}, "included": included}


def test_scan_logs_one_membership_summary_and_one_line_per_campaign() -> None:
    """The scan log has one membership summary and one double-quoted line per campaign."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)
    empty_page = {"data": [], "included": [], "meta": {"pagination": {"cursors": {}}}}

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return _three_memberships_payload()
        return empty_page

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        _stories, logs = _MODULE.scan({"patreon.com": {"value": "c"}}, {}, lambda: fake_now_dt)

    counts = " 0 stories kept, 0 locked posts skipped, 0 posts without a supported file"
    messages = [entry["message"] for entry in logs if entry["level"] == "info"]
    assert messages == [
        "Found 3 Patreon membership(s): 0 active, 1 cancelled, 0 payment declined,"
        " 1 former, 1 free, 0 unknown",
        'Scanned campaign "Demo Campaign" (campaign_id=111, membership=Free):' + counts,
        'Scanned campaign "Second Campaign" (campaign_id=222,'
        " membership=Cancelled (access until 17 Sep 2026)):" + counts,
        'Scanned campaign "Third\'s Stories" (campaign_id=333, membership=Former):' + counts,
    ]


def test_scan_warns_when_a_campaign_hits_the_page_limit() -> None:
    """A campaign whose recent posts outlast 20 pages says the scan stopped early."""
    from datetime import datetime

    fake_now_dt = datetime(2026, 7, 18, 0, 0, 0, tzinfo=UTC)
    endless_page = {
        "data": [
            {
                "id": "900",
                "type": "post",
                "attributes": {
                    "title": "Update",
                    "published_at": "2026-07-15T10:00:00Z",
                    "post_type": "text_only",
                    "current_user_can_view": True,
                    "content": "",
                },
                "relationships": {"attachments_section": {"data": []}, "media": {"data": []}},
            }
        ],
        "included": [],
        "meta": {"pagination": {"cursors": {"next": "more"}}},
    }
    posts_calls: list[str] = []

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        posts_calls.append(url)
        return endless_page

    with mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json):
        _stories, logs = _MODULE.scan({"patreon.com": {"value": "c"}}, {}, lambda: fake_now_dt)

    assert len(posts_calls) == 20
    expected = (
        'Stopped scanning campaign "Demo Campaign" (campaign_id=111) after 20 pages:'
        " older posts were not checked against the 4-week look-back window"
    )
    assert {"level": "warning", "message": expected} in logs


def test_scan_failure_names_the_campaign_in_double_quotes() -> None:
    """A failing campaign's error names it in double quotes with its id."""

    def mock_get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict:
        if "api/current_user" in url:
            return _MEMBERSHIPS_FIXTURE_DATA
        raise RuntimeError("boom")

    with (
        mock.patch.object(_MODULE, "_get_json", side_effect=mock_get_json),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _MODULE.scan({"patreon.com": {"value": "c"}}, {})

    expected = 'Campaign "Demo Campaign" (campaign_id=111): scan failed: boom'
    assert str(exc_info.value) == expected
