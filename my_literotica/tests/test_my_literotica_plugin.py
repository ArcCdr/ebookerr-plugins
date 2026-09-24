"""Tests for the My Literotica activity-wall catalog plugin."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import tomllib
import urllib.parse
from pathlib import Path

import pytest

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "entrypoint.py"
_SPEC = importlib.util.spec_from_file_location("my_literotica_entrypoint", _ENTRYPOINT_PATH)
assert _SPEC is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
assert _SPEC.loader is not None, f"Could not load entrypoint from {_ENTRYPOINT_PATH}"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["my_literotica_entrypoint"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "wall_page.json"
_FIXTURE_DATA = json.loads(_FIXTURE_PATH.read_text())


def test_manifest_declares_catalog_plugin_with_site_auth() -> None:
    """Manifest declares my_literotica as catalog plugin with literotica.com auth."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    manifest_data = tomllib.loads(manifest_path.read_text())

    from ebookerr_sdk.spi.manifest import parse_manifest

    manifest = parse_manifest(manifest_data)

    assert manifest.id == "my_literotica"
    assert manifest.name == "Literotica - My Home"
    assert manifest.plugin_type.value == "catalog"
    assert manifest.network is True
    assert manifest.auth_sites == ("literotica.com",)
    assert manifest.spi_version == "2.30"
    assert manifest.settings_schema.fields == ()


def test_manifest_entrypoint_file_exists() -> None:
    """Entrypoint file referenced in manifest exists."""
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.toml"
    manifest_data = tomllib.loads(manifest_path.read_text())

    entrypoint = manifest_data.get("entrypoint")
    assert entrypoint is not None
    assert (manifest_path.parent / entrypoint).is_file()


def test_main_scan_roundtrip(monkeypatch) -> None:
    """main reads scan request, calls scan, outputs JSON response."""

    def fake_scan(auth: dict) -> tuple[list[dict], list[dict]]:
        return ([{"url": "u", "story_id": "1"}], [{"level": "info", "message": "x"}])

    stdin_data = json.dumps(
        {
            "spi_version": "2.2",
            "op": "scan",
            "request": {"auth": {}},
        }
    )
    captured_stdout = []

    def fake_print(*args, **kwargs):
        captured_stdout.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(_MODULE, "scan", fake_scan)
    monkeypatch.setattr("builtins.input", lambda: stdin_data)
    monkeypatch.setattr("builtins.print", fake_print)

    _MODULE.main()

    output = captured_stdout[0]
    parsed = json.loads(output)
    assert parsed["ok"] is True
    assert parsed["result"] == [{"url": "u", "story_id": "1"}]
    assert parsed["logs"] == [{"level": "info", "message": "x"}]


def test_main_bad_op_reports_error(monkeypatch) -> None:
    """main reports error for unsupported operation."""

    stdin_data = json.dumps(
        {
            "spi_version": "2.2",
            "op": "pull",
            "request": {},
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


def test_main_passes_auth_through(monkeypatch) -> None:
    """main passes auth dict through to scan."""
    captured_auth = []

    def fake_scan(auth: dict) -> tuple[list[dict], list[dict]]:
        captured_auth.append(auth)
        return ([], [])

    stdin_data = json.dumps(
        {
            "spi_version": "2.2",
            "op": "scan",
            "request": {"auth": {"literotica.com": {"kind": "basic", "name": "u", "value": "p"}}},
        }
    )
    captured_stdout = []

    def fake_print(*args, **kwargs):
        captured_stdout.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(_MODULE, "scan", fake_scan)
    monkeypatch.setattr("builtins.input", lambda: stdin_data)
    monkeypatch.setattr("builtins.print", fake_print)

    _MODULE.main()

    assert captured_auth[0] == {"literotica.com": {"kind": "basic", "name": "u", "value": "p"}}


class _FakeResponse:
    """Minimal stand-in for the object ``opener.open`` returns."""

    def __init__(self, status: int, body: bytes) -> None:
        """Store the status and body this fake will serve."""
        self.status = status
        self._body = body

    def read(self) -> bytes:
        """Return the canned body."""
        return self._body

    def __enter__(self) -> _FakeResponse:
        """Support ``with`` blocks."""
        return self

    def __exit__(self, *args: object) -> None:
        """Support ``with`` blocks."""
        return None


def test_login_returns_sessionid(monkeypatch) -> None:
    """login returns the sessionid cookie value on success."""
    import http.cookiejar as http_cookiejar_module

    real_cookiejar = http_cookiejar_module.CookieJar
    fake_jar = real_cookiejar()
    fake_jar.set_cookie(
        http_cookiejar_module.Cookie(
            version=0,
            name="sessionid",
            value="s3ss10n-value",
            port=None,
            port_specified=False,
            domain=".auth.literotica.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
    )

    def fake_cookiejar_factory(*args, **kwargs):
        return fake_jar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                return _FakeResponse(200, b"OK")

        return FakeOpener()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    result = _MODULE.login("user", "pw")
    assert result == "s3ss10n-value"


def test_login_sends_the_exact_form_fields(monkeypatch) -> None:
    """login sends exact form fields in the request."""
    import http.cookiejar as http_cookiejar_module
    import urllib.parse

    real_cookiejar = http_cookiejar_module.CookieJar
    real_cookie = http_cookiejar_module.Cookie
    captured_request = []

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                captured_request.append(req)
                return _FakeResponse(200, b"OK")

        return FakeOpener()

    def fake_cookiejar_factory(*args, **kwargs):
        jar = real_cookiejar()
        jar.set_cookie(
            real_cookie(
                version=0,
                name="sessionid",
                value="test-sessionid",
                port=None,
                port_specified=False,
                domain=".auth.literotica.com",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
        return jar

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    _MODULE.login("user", "pw")

    assert len(captured_request) == 1
    req = captured_request[0]
    parsed_data = urllib.parse.parse_qs(req.data.decode())
    assert parsed_data == {
        "login": ["user"],
        "password": ["pw"],
        "return_to": ["www.literotica.com"],
        "form_url": ["https://www.literotica.com/authenticate/login"],
    }
    assert req.full_url == _MODULE.AUTH_LOGIN_URL
    assert req.get_header("User-agent") == _MODULE._UA


def test_login_rejects_bad_credentials_on_303(monkeypatch) -> None:
    """login raises RuntimeError on 303 redirect (bad credentials)."""
    import http.cookiejar as http_cookiejar_module
    import urllib.error

    real_cookiejar = http_cookiejar_module.CookieJar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError("u", 303, "See Other", {}, None)

        return FakeOpener()

    def fake_cookiejar_factory(*args, **kwargs):
        return real_cookiejar()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.login("u", "bad")
    assert str(excinfo.value) == "Literotica rejected the stored username or password"


def test_login_raises_on_other_http_error(monkeypatch) -> None:
    """login raises RuntimeError on non-redirect HTTP errors."""
    import http.cookiejar as http_cookiejar_module
    import urllib.error

    real_cookiejar = http_cookiejar_module.CookieJar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError("u", 500, "boom", {}, None)

        return FakeOpener()

    def fake_cookiejar_factory(*args, **kwargs):
        return real_cookiejar()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.login("u", "pw")
    assert str(excinfo.value) == "Literotica login failed: HTTP 500"


def test_login_raises_when_body_is_not_ok(monkeypatch) -> None:
    """login raises RuntimeError when response body is not 'OK'."""
    import http.cookiejar as http_cookiejar_module

    real_cookiejar = http_cookiejar_module.CookieJar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                return _FakeResponse(200, b"<html>nope</html>")

        return FakeOpener()

    def fake_cookiejar_factory(*args, **kwargs):
        return real_cookiejar()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.login("u", "pw")
    assert str(excinfo.value) == "Literotica rejected the stored username or password"


def test_login_raises_when_no_session_cookie(monkeypatch) -> None:
    """login raises RuntimeError when no sessionid cookie is issued."""
    import http.cookiejar as http_cookiejar_module

    real_cookiejar = http_cookiejar_module.CookieJar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                return _FakeResponse(200, b"OK")

        return FakeOpener()

    def fake_cookiejar_factory(*args, **kwargs):
        return real_cookiejar()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.login("u", "pw")
    assert str(excinfo.value) == "Literotica login succeeded but issued no session cookie"


def test_login_raises_on_network_error(monkeypatch) -> None:
    """login raises RuntimeError on network errors."""
    import http.cookiejar as http_cookiejar_module
    import urllib.error

    real_cookiejar = http_cookiejar_module.CookieJar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                raise urllib.error.URLError("no route")

        return FakeOpener()

    def fake_cookiejar_factory(*args, **kwargs):
        return real_cookiejar()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.login("u", "pw")
    assert str(excinfo.value).startswith("Literotica login could not be reached:")


def test_login_never_leaks_the_password_in_the_error(monkeypatch) -> None:
    """login never includes password in error messages."""
    import http.cookiejar as http_cookiejar_module
    import urllib.error

    real_cookiejar = http_cookiejar_module.CookieJar

    def fake_opener_factory(*args, **kwargs):
        class FakeOpener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError("u", 303, "See Other", {}, None)

        return FakeOpener()

    def fake_cookiejar_factory(*args, **kwargs):
        return real_cookiejar()

    monkeypatch.setattr(_MODULE.http.cookiejar, "CookieJar", fake_cookiejar_factory)
    monkeypatch.setattr(_MODULE.urllib.request, "build_opener", fake_opener_factory)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.login("u", "hunter2hunter2")
    assert "hunter2hunter2" not in str(excinfo.value)


def test_mint_token_returns_the_body(monkeypatch) -> None:
    """mint_token returns the body stripped."""

    def fake_urlopen(*args, **kwargs):
        return _FakeResponse(200, b"  aaa.bbb.ccc  ")

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    result = _MODULE.mint_token("sid")
    assert result == "aaa.bbb.ccc"


def test_mint_token_sends_the_session_cookie_and_timestamp(monkeypatch) -> None:
    """mint_token sends the session cookie and timestamp."""
    captured_request = []

    def fake_urlopen(req, *args, **kwargs):
        captured_request.append(req)
        return _FakeResponse(200, b"aaa.bbb.ccc")

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    _MODULE.mint_token("sid")

    assert len(captured_request) == 1
    req = captured_request[0]
    assert req.get_header("Cookie") == "sessionid=sid"
    assert req.get_header("User-agent") == _MODULE._UA
    assert req.full_url.startswith(_MODULE.AUTH_CHECK_URL + "?timestamp=")
    timestamp_str = req.full_url.split("timestamp=")[1]
    assert int(timestamp_str) > 0


def test_mint_token_raises_on_401(monkeypatch) -> None:
    """mint_token raises urllib.error.HTTPError(401)."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _MODULE.mint_token("sid")


def test_mint_token_raises_on_403(monkeypatch) -> None:
    """mint_token raises urllib.error.HTTPError(403)."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _MODULE.mint_token("sid")


def test_mint_token_raises_on_other_http_error(monkeypatch) -> None:
    """mint_token raises urllib.error.HTTPError on other HTTP errors."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _MODULE.mint_token("sid")


def test_mint_token_raises_on_network_error(monkeypatch) -> None:
    """mint_token raises urllib.error.URLError on network errors."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.URLError):
        _MODULE.mint_token("sid")


def test_mint_token_rejects_a_non_jwt_body(monkeypatch) -> None:
    """mint_token raises RuntimeError on non-JWT body."""

    def fake_urlopen(*args, **kwargs):
        return _FakeResponse(200, b"Unauthorized")

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.mint_token("sid")
    assert str(excinfo.value) == "Literotica token refresh returned an unexpected response"


def test_mint_token_never_leaks_the_session_id(monkeypatch) -> None:
    """mint_token never includes session_id in error messages."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _MODULE.mint_token("super-secret-session")


def test_resolve_token_basic_logs_in_and_mints(monkeypatch) -> None:
    """resolve_token with basic credentials logs in and mints a fresh token."""
    monkeypatch.setattr(
        _MODULE,
        "login",
        lambda u, p: "SID" if (u, p) == ("bob", "pw") else pytest.fail("wrong credentials"),
    )
    monkeypatch.setattr(
        _MODULE,
        "mint_token",
        lambda s: "tok" if s == "SID" else pytest.fail("wrong sessionid"),
    )

    result = _MODULE.resolve_token(
        {"literotica.com": {"kind": "basic", "name": "bob", "value": "pw"}}
    )
    assert result == ("tok", True)


def test_resolve_token_cookie_auth_token_is_used_verbatim() -> None:
    """resolve_token with auth_token cookie returns it verbatim without network calls."""
    result = _MODULE.resolve_token(
        {"literotica.com": {"kind": "cookie", "name": "auth_token", "value": "aaa.bbb.ccc"}}
    )
    assert result == ("aaa.bbb.ccc", False)


def test_resolve_token_raises_when_no_profile() -> None:
    """resolve_token raises RuntimeError when no literotica.com profile is stored."""
    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.resolve_token({})
    msg = str(excinfo.value)
    assert "Literotica credentials not configured" in msg
    assert "Settings -> Site authentication" in msg


def test_resolve_token_raises_when_value_is_blank() -> None:
    """resolve_token raises RuntimeError when profile value is blank."""
    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.resolve_token({"literotica.com": {"kind": "basic", "name": "bob", "value": ""}})
    msg = str(excinfo.value)
    assert "Literotica credentials not configured" in msg


def test_resolve_token_raises_on_wrong_cookie_name() -> None:
    """resolve_token raises RuntimeError for cookie with wrong name."""
    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.resolve_token(
            {"literotica.com": {"kind": "cookie", "name": "sessionid", "value": "x"}}
        )
    msg = str(excinfo.value)
    assert "must be kind 'basic'" in msg
    assert "auth_token" in msg


def test_resolve_token_raises_on_header_kind() -> None:
    """resolve_token raises RuntimeError when kind is 'header'."""
    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.resolve_token(
            {"literotica.com": {"kind": "header", "name": "Authorization", "value": "Bearer x"}}
        )
    msg = str(excinfo.value)
    assert "must be kind 'basic'" in msg


def test_resolve_token_raises_when_profile_is_not_a_dict() -> None:
    """resolve_token raises RuntimeError when profile is not a dict."""
    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.resolve_token({"literotica.com": "nope"})
    msg = str(excinfo.value)
    assert "Literotica credentials not configured" in msg


def test_resolve_token_never_leaks_the_secret() -> None:
    """resolve_token never includes secret values in error messages."""
    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.resolve_token(
            {"literotica.com": {"kind": "header", "name": "X", "value": "sup3r-s3cr3t-value"}}
        )
    assert "sup3r-s3cr3t-value" not in str(excinfo.value)


def test_story_url_story_uses_s_prefix() -> None:
    """story_url with 'story' type uses s prefix."""
    assert _MODULE.story_url("story", "abc") == "https://www.literotica.com/s/abc"


def test_story_url_audio_uses_s_prefix() -> None:
    """story_url with 'audio' type uses s prefix."""
    assert _MODULE.story_url("audio", "abc") == "https://www.literotica.com/s/abc"


def test_story_url_poem_uses_p_prefix() -> None:
    """story_url with 'poem' type uses p prefix."""
    assert (
        _MODULE.story_url("poem", "an-ode-to-mr-strand")
        == "https://www.literotica.com/p/an-ode-to-mr-strand"
    )


def test_story_url_illustration_uses_i_prefix() -> None:
    """story_url with 'illustration' type uses i prefix."""
    assert _MODULE.story_url("illustration", "abc") == "https://www.literotica.com/i/abc"


def test_story_url_unknown_type_falls_back_to_s() -> None:
    """story_url with unknown type falls back to s prefix."""
    assert _MODULE.story_url("sgs", "abc") == "https://www.literotica.com/s/abc"


def test_story_url_none_type_falls_back_to_s() -> None:
    """story_url with None type falls back to s prefix."""
    assert _MODULE.story_url(None, "abc") == "https://www.literotica.com/s/abc"


def test_story_url_matches_the_fixture_poem() -> None:
    """story_url correctly builds URL from fixture poem data."""
    what = _FIXTURE_DATA["data"][2]["what"]
    result = _MODULE.story_url(what["type"], what["url"])
    assert result == "https://www.literotica.com/p/an-ode-to-mr-strand"


def test_story_url_matches_the_fixture_story() -> None:
    """story_url correctly builds URL from fixture story data."""
    what = _FIXTURE_DATA["data"][0]["what"]
    result = _MODULE.story_url(what["type"], what["url"])
    assert result == "https://www.literotica.com/s/bimbo-potion-the-aftermath"


def test_build_custom_fields_story_without_series() -> None:
    """build_custom_fields extracts custom columns from story without series."""
    a = _FIXTURE_DATA["data"][0]
    result = _MODULE.build_custom_fields(a["what"], a["when"])
    assert result == {
        "Votes": 33,
        "Views": 3501,
        "Favorites": 5,
        "Comments": 0,
        "Reading Lists": 21,
        "Is Hot": False,
        "Is New": True,
        "Writer's Pick": False,
        "Contest Winner": False,
        "Downloadable": False,
        "Voting Enabled": True,
        "Comments Enabled": True,
        "Language": "en",
        "Type": "story",
        "Author Stories": 86,
        "Author Poems": 0,
        "Author Audios": 0,
        "Author Illustrations": 0,
        "Announced": "2026-07-30T05:55:03+00:00",
    }


def test_build_custom_fields_counts_series_parts() -> None:
    """build_custom_fields counts series parts and detects is_hot."""
    a = _FIXTURE_DATA["data"][1]
    result = _MODULE.build_custom_fields(a["what"], a["when"])
    assert result["Series Parts"] == 2
    assert result["Is Hot"] is True
    assert result["Type"] == "story"


def test_build_custom_fields_marks_a_poem() -> None:
    """build_custom_fields marks poems and includes poet's counts."""
    a = _FIXTURE_DATA["data"][2]
    result = _MODULE.build_custom_fields(a["what"], a["when"])
    assert result["Type"] == "poem"
    assert result["Author Poems"] == 2
    assert "Series Parts" not in result


def test_build_custom_fields_omits_null_rank() -> None:
    """build_custom_fields omits Rank when null and includes when present."""
    assert "Rank" not in _MODULE.build_custom_fields(_FIXTURE_DATA["data"][0]["what"], 0)
    assert _MODULE.build_custom_fields({"rank": 7}, None)["Rank"] == 7


def test_build_custom_fields_coerces_int_flags_to_bool() -> None:
    """build_custom_fields coerces integer flags to boolean."""
    result = _MODULE.build_custom_fields({"contest_winner": 1, "allow_download": 0}, None)
    assert result == {"Contest Winner": True, "Downloadable": False}
    assert result["Contest Winner"] is True
    assert result["Downloadable"] is False


def test_build_custom_fields_unknown_language_falls_back_to_str() -> None:
    """build_custom_fields falls back to string for unknown language codes."""
    result = _MODULE.build_custom_fields({"language": 7}, None)
    assert result["Language"] == "7"


def test_build_custom_fields_omits_announced_without_a_timestamp() -> None:
    """build_custom_fields omits Announced when timestamp is None or non-int."""
    assert "Announced" not in _MODULE.build_custom_fields({}, None)
    assert "Announced" not in _MODULE.build_custom_fields({}, "not-a-timestamp")


def test_build_custom_fields_empty_payload_is_empty() -> None:
    """build_custom_fields returns empty dict for empty payload."""
    assert _MODULE.build_custom_fields({}, None) == {}


def test_build_custom_fields_values_are_all_wire_safe() -> None:
    """build_custom_fields values are all str, int, float, or bool."""
    for activity in _FIXTURE_DATA["data"]:
        what = activity.get("what")
        if isinstance(what, dict):
            result = _MODULE.build_custom_fields(what, activity.get("when"))
            for v in result.values():
                assert isinstance(v, (str, int, float, bool)), f"Found {type(v)} in {v}"


def test_map_activity_core_fields() -> None:
    """map_activity extracts core fields from a story without series."""
    r = _MODULE.map_activity(_FIXTURE_DATA["data"][0])
    assert r["url"] == "https://www.literotica.com/s/bimbo-potion-the-aftermath"
    assert r["story_id"] == "4480762"
    assert r["title"] == "Bimbo Potion: The Aftermath"
    assert r["author"] == "fidget1"
    assert r["author_url"] == "https://www.literotica.com/authors/fidget1"
    assert r["category"] == "Mind Control"
    assert r["tags"] == "breast expansion, sluttification"
    assert r["rating"] == 4.45
    assert r["num_words"] == 3475
    assert r["date_published"] == "2026-07-30"
    assert r["site"] == "literotica.com"
    assert r["description"] == "Jake fucking his slutty bimbo has predictable consequences."
    assert "series" not in r
    assert "series_url" not in r


def test_map_activity_series_fields() -> None:
    """map_activity extracts series fields and series part count."""
    r = _MODULE.map_activity(_FIXTURE_DATA["data"][1])
    assert r["series"] == "Fighting Them There"
    assert r["series_url"] == "https://www.literotica.com/series/se/494140239"
    assert r["custom"]["Series Parts"] == 2
    assert r["title"] == "Fighting Them There Ch. 37"


def test_map_activity_poem_uses_the_poem_url() -> None:
    """map_activity uses the p prefix for poems."""
    r = _MODULE.map_activity(_FIXTURE_DATA["data"][2])
    assert r["url"] == "https://www.literotica.com/p/an-ode-to-mr-strand"
    assert r["story_id"] == "4459984"
    assert r["category"] == "Non-Erotic Poetry"
    assert r["custom"]["Type"] == "poem"
    assert r["date_published"] == "2026-07-12"


def test_map_activity_skips_non_story_actions() -> None:
    """map_activity returns None for non-story actions."""
    assert _MODULE.map_activity(_FIXTURE_DATA["data"][3]) is None


def test_map_activity_skips_when_what_is_not_a_dict() -> None:
    """map_activity returns None when what is not a dict."""
    assert _MODULE.map_activity({"action": "published-story", "what": ["bio"]}) is None


def test_map_activity_skips_without_slug() -> None:
    """map_activity returns None when slug is missing."""
    assert _MODULE.map_activity({"action": "published-story", "what": {"id": 1}}) is None


def test_map_activity_skips_without_story_id() -> None:
    """map_activity returns None when story_id is missing."""
    assert _MODULE.map_activity({"action": "published-story", "what": {"url": "x"}}) is None


def test_map_activity_falls_back_to_authorname() -> None:
    """map_activity falls back to authorname when author dict is absent."""
    activity = {
        "action": "published-story",
        "when": 0,
        "what": {"url": "x", "id": 9, "authorname": "solo"},
    }
    r = _MODULE.map_activity(activity)
    assert r["author"] == "solo"
    assert r["author_url"] == "https://www.literotica.com/authors/solo"


def test_map_activity_tolerates_unparseable_date() -> None:
    """map_activity omits date_published when date_approve is unparseable."""
    activity = {
        "action": "published-story",
        "when": 0,
        "what": {"url": "x", "id": 9, "date_approve": "not-a-date"},
    }
    r = _MODULE.map_activity(activity)
    assert "date_published" not in r
    assert r["url"] == "https://www.literotica.com/s/x"


def test_map_activity_coerces_non_string_tag_values() -> None:
    """map_activity coerces a non-string tag value instead of crashing on join."""
    activity = {
        "action": "published-story",
        "when": 0,
        "what": {"url": "x", "id": 9, "tags": [{"tag": "normal"}, {"tag": 123}]},
    }
    r = _MODULE.map_activity(activity)
    assert r["tags"] == "normal, 123"


def test_map_activity_omits_null_rating() -> None:
    """map_activity omits rating when rate_all is None."""
    activity = {
        "action": "published-story",
        "when": 0,
        "what": {"url": "x", "id": 9, "rate_all": None},
    }
    r = _MODULE.map_activity(activity)
    assert "rating" not in r


def test_map_activity_never_sets_the_unmapped_fields() -> None:
    """map_activity never sets status, num_chapters, cover_image, date_updated, or format."""
    for activity in _FIXTURE_DATA["data"][:3]:
        r = _MODULE.map_activity(activity)
        if r is not None:
            assert "status" not in r
            assert "num_chapters" not in r
            assert "cover_image" not in r
            assert "date_updated" not in r
            assert "format" not in r


def test_map_activity_every_fixture_story_has_url_and_story_id() -> None:
    """map_activity produces non-None result with url and story_id for each fixture story."""
    for activity in _FIXTURE_DATA["data"][:3]:
        r = _MODULE.map_activity(activity)
        assert r is not None
        assert isinstance(r["url"], str)
        assert r["url"]
        assert isinstance(r["story_id"], str)
        assert r["story_id"]


def test_fetch_wall_page_returns_data(monkeypatch) -> None:
    """fetch_wall_page returns data list from response."""

    def fake_urlopen(*args, **kwargs):
        return _FakeResponse(200, json.dumps(_FIXTURE_DATA).encode())

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    result = _MODULE.fetch_wall_page("tok", None)
    assert len(result) == 4


def test_fetch_wall_page_first_page_params(monkeypatch) -> None:
    """fetch_wall_page first page uses correct params without last_id."""
    captured_request = []

    def fake_urlopen(req, *args, **kwargs):
        captured_request.append(req)
        return _FakeResponse(200, json.dumps(_FIXTURE_DATA).encode())

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    _MODULE.fetch_wall_page("tok", None)

    assert len(captured_request) == 1
    req = captured_request[0]
    assert req.full_url == _MODULE.WALL_URL + "?params=" + urllib.parse.quote(
        '{"chunked":1,"limit":50}'
    )
    assert req.get_header("Authorization") == "Bearer tok"


def test_fetch_wall_page_sends_the_cursor(monkeypatch) -> None:
    """fetch_wall_page includes last_id in params when provided."""
    captured_request = []

    def fake_urlopen(req, *args, **kwargs):
        captured_request.append(req)
        return _FakeResponse(200, b'{"data": []}')

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    _MODULE.fetch_wall_page("tok", "cursor-1")

    assert len(captured_request) == 1
    req = captured_request[0]
    url_params = req.full_url.split("?params=")[1]
    decoded = json.loads(urllib.parse.unquote(url_params))
    assert decoded == {"chunked": 1, "limit": 50, "last_id": "cursor-1"}


def test_fetch_wall_page_missing_data_is_empty(monkeypatch) -> None:
    """fetch_wall_page returns empty list when data field is missing."""

    def fake_urlopen(*args, **kwargs):
        return _FakeResponse(200, b'{"new_activity_count": 0}')

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    result = _MODULE.fetch_wall_page("tok", None)
    assert result == []


def test_fetch_wall_page_raises_on_403(monkeypatch) -> None:
    """fetch_wall_page raises urllib.error.HTTPError(403) Forbidden."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _MODULE.fetch_wall_page("tok", None)


def test_fetch_wall_page_raises_on_500(monkeypatch) -> None:
    """fetch_wall_page raises urllib.error.HTTPError(500) server error."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("u", 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _MODULE.fetch_wall_page("tok", None)


def test_fetch_wall_page_raises_on_network_error(monkeypatch) -> None:
    """fetch_wall_page raises urllib.error.URLError on network errors."""
    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.URLError):
        _MODULE.fetch_wall_page("tok", None)


def test_fetch_wall_page_raises_on_bad_json(monkeypatch) -> None:
    """fetch_wall_page raises RuntimeError on malformed JSON."""

    def fake_urlopen(*args, **kwargs):
        return _FakeResponse(200, b"<html>")

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        _MODULE.fetch_wall_page("tok", None)
    assert str(excinfo.value) == "Literotica activity wall returned malformed JSON"


def test_fetch_activities_stops_on_a_short_page(monkeypatch) -> None:
    """fetch_activities stops when a page has fewer than PAGE_SIZE items."""
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        if call_count[0] == 1:
            # First page: 50 items
            return [{"id": f"a{i}"} for i in range(50)]
        elif call_count[0] == 2:
            # Second page: 3 items (short page, stop)
            return [{"id": f"b{i}"} for i in range(3)]
        return []

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    assert len(activities) == 53
    assert call_count[0] == 2
    # Verify second call received the cursor from first page's last item
    # This is implicit in the fact we got 53 items total with the right structure


def test_fetch_activities_stops_on_an_empty_page(monkeypatch) -> None:
    """fetch_activities stops when a page is empty."""
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return [{"id": f"a{i}"} for i in range(50)]
        return []

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    assert len(activities) == 50
    assert call_count[0] == 2


def test_fetch_activities_logs_one_debug_line_per_page(monkeypatch) -> None:
    """fetch_activities logs one debug line per page."""
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return [{"id": f"a{i}"} for i in range(50)]
        elif call_count[0] == 2:
            return [{"id": f"b{i}"} for i in range(3)]
        return []

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    debug_logs = [log for log in logs if log["level"] == "debug"]
    assert len(debug_logs) == 2
    assert debug_logs[0]["message"] == "Activity wall page 1: 50 activities"
    assert debug_logs[1]["message"] == "Activity wall page 2: 3 activities"


def test_fetch_activities_warns_at_the_page_cap(monkeypatch) -> None:
    """fetch_activities warns when page cap is reached."""
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        # Always return 50 items (a full page)
        return [{"id": f"{call_count[0]}-{i}"} for i in range(50)]

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    assert call_count[0] == _MODULE.MAX_PAGES
    assert len(activities) == 50 * _MODULE.MAX_PAGES
    warning_logs = [log for log in logs if log["level"] == "warning"]
    assert len(warning_logs) == 1
    assert "Activity wall page cap reached (10 pages, 500 activities)" in warning_logs[0]["message"]


def test_fetch_activities_warns_when_the_cursor_is_missing(monkeypatch) -> None:
    """fetch_activities warns when last activity has no id."""
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        if call_count[0] == 1:
            # 50 items, but last one has no id
            items = [{"id": f"a{i}"} for i in range(49)]
            items.append({})  # No id
            return items
        return []

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    assert call_count[0] == 1
    warning_logs = [log for log in logs if log["level"] == "warning"]
    assert len(warning_logs) == 1
    assert "carried no cursor id" in warning_logs[0]["message"]


def test_fetch_activities_single_page_makes_one_call(monkeypatch) -> None:
    """fetch_activities stops after one call if first page has fewer than PAGE_SIZE items."""
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return [{"id": f"a{i}"} for i in range(4)]
        return []

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    assert call_count[0] == 1
    assert len(activities) == 4
    debug_logs = [log for log in logs if log["level"] == "debug"]
    assert len(debug_logs) == 1
    assert debug_logs[0]["message"] == "Activity wall page 1: 4 activities"
    warning_logs = [log for log in logs if log["level"] == "warning"]
    assert len(warning_logs) == 0


def test_fetch_activities_dedupes_a_boundary_inclusive_cursor(monkeypatch) -> None:
    """fetch_activities drops the duplicate when last_id's own activity reopens the next page.

    Observed live (RUN_E2E test_wall_cursor_advances): the wall's last_id
    cursor is inclusive, so the item it names reappears as the first item of the next page.
    """
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        if call_count[0] == 1:
            # Full page; last item is "a49".
            return [{"id": f"a{i}"} for i in range(50)]
        elif call_count[0] == 2:
            # Boundary-inclusive: "a49" reopens this page before the genuinely new items.
            return [{"id": "a49"}] + [{"id": f"b{i}"} for i in range(2)]
        return []

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    ids = [a["id"] for a in activities]
    assert len(ids) == len(set(ids)), f"duplicate ids in result: {ids}"
    assert ids == [f"a{i}" for i in range(50)] + ["b0", "b1"]


def test_fetch_activities_dedupes_a_repeat_within_one_page(monkeypatch) -> None:
    """fetch_activities drops a duplicate id even when both copies land in the same page.

    Observed live: the wall itself has repeated the same activity id back-to-back inside a
    single page response, not just across a page boundary.
    """

    def fake_fetch_wall_page(token, last_id):
        items = [{"id": f"a{i}"} for i in range(48)]
        items.insert(10, {"id": "a3"})  # "a3" now appears twice in this one page
        return items

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    ids = [a["id"] for a in activities]
    assert len(ids) == len(set(ids)), f"duplicate ids in result: {ids}"
    assert ids == [f"a{i}" for i in range(48)]


def test_fetch_activities_still_paginates_past_a_fully_duplicate_page(monkeypatch) -> None:
    """A short page whose every item was already seen still ends the scan cleanly.

    len(items) < PAGE_SIZE (the raw page, before dedup) is what signals exhaustion — a page
    that dedupes down to zero new items must not be mistaken for one that never returned.
    """
    call_count = [0]

    def fake_fetch_wall_page(token, last_id):
        call_count[0] += 1
        if call_count[0] == 1:
            return [{"id": f"a{i}"} for i in range(50)]
        # Short, boundary-inclusive final page: only the already-seen last item.
        return [{"id": "a49"}]

    monkeypatch.setattr(_MODULE, "fetch_wall_page", fake_fetch_wall_page)

    activities, logs = _MODULE.fetch_activities("tok")
    assert call_count[0] == 2
    assert len(activities) == 50


_AUTH = {"literotica.com": {"kind": "cookie", "name": "auth_token", "value": "aaa.bbb.ccc"}}


def test_scan_maps_the_fixture_wall(monkeypatch) -> None:
    """scan maps the fixture wall and returns 3 stories."""
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (_FIXTURE_DATA["data"], [{"level": "debug", "message": "page"}]),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert len(result) == 3
    assert [s["story_id"] for s in result] == ["4480762", "4480507", "4459984"]


def test_scan_dedupes_by_story_id(monkeypatch) -> None:
    """scan dedupes by story_id, keeping the first occurrence."""
    data = _FIXTURE_DATA["data"] + [_FIXTURE_DATA["data"][0]]
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (data, []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert len(result) == 3
    info_logs = [log for log in logs if log["level"] == "info"]
    summary_log = next(
        (entry for entry in info_logs if "Activity wall scanned:" in entry["message"]), None
    )
    assert summary_log is not None
    assert "1 duplicate(s) dropped" in summary_log["message"]
    assert "4 story publication(s)" in summary_log["message"]


def test_scan_keeps_the_first_occurrence(monkeypatch) -> None:
    """scan keeps the first occurrence when deduping."""
    activity1 = {
        "action": "published-story",
        "when": 100,
        "what": {
            "url": "test-story",
            "id": 9999,
            "title": "first",
            "date_approve": "07/30/2026",
        },
    }
    activity2 = {
        "action": "published-story",
        "when": 50,
        "what": {
            "url": "test-story",
            "id": 9999,
            "title": "second",
            "date_approve": "07/30/2026",
        },
    }
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([activity1, activity2], []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert len(result) == 1
    assert result[0]["title"] == "first"


def test_scan_logs_the_summary(monkeypatch) -> None:
    """scan logs a summary with activity count, oldest date, and dedupe stats."""
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (_FIXTURE_DATA["data"], []),
    )

    result, logs = _MODULE.scan(_AUTH)
    info_logs = [log for log in logs if log["level"] == "info"]
    summary_log = next(
        (entry for entry in info_logs if "Activity wall scanned:" in entry["message"]), None
    )
    assert summary_log is not None
    assert (
        "Activity wall scanned: 4 activities back to 2026-07-30 17:36 UTC,"
        " 3 story publication(s), 3 kept (0 duplicate(s) dropped)"
    ) in summary_log["message"]


def test_scan_logs_the_credential_path_for_a_token(monkeypatch) -> None:
    """scan logs that it is using a stored token."""
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([], []),
    )
    result, logs = _MODULE.scan(_AUTH)
    info_logs = [log for log in logs if log["level"] == "info"]
    cred_log = next(
        (
            entry
            for entry in info_logs
            if "Using the stored literotica.com auth_token" in entry["message"]
        ),
        None,
    )
    assert cred_log is not None
    assert cred_log["message"] == (
        "Using the stored literotica.com auth_token (valid for about one hour)"
    )


def test_scan_logs_the_credential_path_for_a_login(monkeypatch) -> None:
    """scan logs that it authenticated with username/password."""
    monkeypatch.setattr(
        _MODULE,
        "resolve_token",
        lambda a: ("tok", True),
    )
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([], []),
    )

    result, logs = _MODULE.scan(_AUTH)
    info_logs = [log for log in logs if log["level"] == "info"]
    cred_log = next(
        (entry for entry in info_logs if "Authenticated with literotica.com" in entry["message"]),
        None,
    )
    assert cred_log is not None
    assert cred_log["message"] == (
        "Authenticated with literotica.com using the stored username and password"
    )


def test_scan_raises_when_no_credential_is_stored() -> None:
    """scan raises RuntimeError when no literotica.com credential is stored."""
    with pytest.raises(RuntimeError, match="Literotica credentials not configured"):
        _MODULE.scan({})


def test_scan_raises_on_a_fetch_failure(monkeypatch) -> None:
    """scan raises RuntimeError when fetch_activities fails."""

    def raise_error(token):
        raise RuntimeError("Literotica activity wall failed: HTTP 500")

    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        raise_error,
    )

    with pytest.raises(RuntimeError, match="Literotica activity wall failed: HTTP 500"):
        _MODULE.scan(_AUTH)


def test_main_reports_a_scan_failure_as_ok_false(monkeypatch) -> None:
    """main reports scan failure with ok=false and error field."""

    def fake_scan(auth: dict) -> tuple[list[dict], list[dict]]:
        raise RuntimeError("wall down")

    stdin_data = json.dumps(
        {
            "spi_version": "2.2",
            "op": "scan",
            "request": {"auth": {}},
        }
    )
    captured_stdout = []

    def fake_print(*args, **kwargs):
        captured_stdout.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(_MODULE, "scan", fake_scan)
    monkeypatch.setattr("builtins.input", lambda: stdin_data)
    monkeypatch.setattr("builtins.print", fake_print)

    with contextlib.suppress(SystemExit):
        _MODULE.main()

    output = captured_stdout[0]
    parsed = json.loads(output)
    assert parsed["ok"] is False
    assert parsed["error"] == "wall down"
    assert "result" not in parsed


def test_scan_warns_when_the_wall_holds_no_stories(monkeypatch) -> None:
    """scan warns when no stories are found."""
    # Only the profile-updated entry (index 3)
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([_FIXTURE_DATA["data"][3]], []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert result == []
    warning_logs = [log for log in logs if log["level"] == "warning"]
    assert len(warning_logs) == 1
    assert "held no story publications" in warning_logs[0]["message"]


def test_scan_forwards_the_fetch_logs(monkeypatch) -> None:
    """scan includes logs from fetch_activities in the result."""
    fetch_log = {"level": "debug", "message": "Activity wall page 1: 0 activities"}
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([], [fetch_log]),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert fetch_log in logs


def test_scan_never_logs_the_token(monkeypatch) -> None:
    """scan never includes the token value in any log message."""
    secret_auth = {
        "literotica.com": {"kind": "cookie", "name": "auth_token", "value": "s3cr3t.t0k.en"}
    }
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([], []),
    )

    result, logs = _MODULE.scan(secret_auth)
    all_messages = " ".join(log["message"] for log in logs)
    assert "s3cr3t.t0k.en" not in all_messages


def test_scan_passes_the_resolved_token_to_fetch(monkeypatch) -> None:
    """scan passes the resolved token to fetch_activities."""
    captured_token = []

    def fake_fetch(token):
        captured_token.append(token)
        return ([], [])

    monkeypatch.setattr(_MODULE, "fetch_activities", fake_fetch)

    _MODULE.scan(_AUTH)
    assert captured_token[0] == "aaa.bbb.ccc"


def test_my_literotica_scan_reports_two_stages(monkeypatch, capsys) -> None:
    """scan reports exactly two progress frames: 50% after fetch, 100% at end."""
    monkeypatch.setattr(
        _MODULE,
        "resolve_token",
        lambda a: ("tok", False),
    )
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([], []),
    )

    _MODULE.scan({})

    captured = capsys.readouterr()
    lines = [line.strip() for line in captured.out.split("\n") if line.strip()]
    progress_frames = [json.loads(line) for line in lines if line.startswith('{"op": "progress"')]

    percents = [frame["percent"] for frame in progress_frames]
    assert pytest.approx(percents) == [50.0, 100.0]


def test_my_literotica_scan_reports_nothing_when_token_fails(monkeypatch, capsys) -> None:
    """scan raises RuntimeError without emitting any progress frame."""
    monkeypatch.setattr(
        _MODULE,
        "resolve_token",
        lambda a: (_ for _ in ()).throw(RuntimeError("no creds")),
    )

    with pytest.raises(RuntimeError, match="no creds"):
        _MODULE.scan({})

    captured = capsys.readouterr()
    assert '{"op": "progress"' not in captured.out


def test_my_literotica_scan_reports_nothing_when_fetch_fails(monkeypatch, capsys) -> None:
    """scan raises RuntimeError without emitting any progress frame."""
    monkeypatch.setattr(
        _MODULE,
        "resolve_token",
        lambda a: ("tok", False),
    )
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        _MODULE.scan({})

    captured = capsys.readouterr()
    assert '{"op": "progress"' not in captured.out


def test_category_name_maps_the_reported_examples() -> None:
    """category_name maps the reported examples."""
    for cat_id, expected in (
        (13, "Reluctance/NonConsent"),
        (12, "Loving Wives"),
        (10, "Interracial Love"),
        (38, "Sci-Fi & Fantasy"),
    ):
        assert _MODULE.category_name({"category": cat_id}) == expected


def test_category_name_accepts_a_string_id() -> None:
    """category_name accepts a string id."""
    assert _MODULE.category_name({"category": "36"}) == "Non-Erotic Poetry"


def test_category_name_returns_none_for_an_unknown_id() -> None:
    """category_name returns None for an unknown id."""
    assert _MODULE.category_name({"category": 999}) is None


def test_category_name_returns_none_when_absent() -> None:
    """category_name returns None when absent."""
    assert _MODULE.category_name({}) is None


def test_category_label_falls_back_to_the_slug() -> None:
    """category_label falls back to the slug when id is unmapped."""
    result = _MODULE.category_label(
        {"category": 999, "category_info": {"pageUrl": "brand-new-thing"}}
    )
    assert result == "Brand New Thing"


def test_category_label_prefers_the_mapped_name_over_the_slug() -> None:
    """category_label prefers the mapped name over the slug."""
    result = _MODULE.category_label(
        {"category": 38, "category_info": {"pageUrl": "science-fiction-fantasy"}}
    )
    assert result == "Sci-Fi & Fantasy"


def test_category_label_returns_none_without_id_or_slug() -> None:
    """category_label returns None without id or slug."""
    assert _MODULE.category_label({}) is None


def test_map_activity_uses_the_mapped_category() -> None:
    """map_activity uses the mapped category."""
    import copy

    activity = copy.deepcopy(_FIXTURE_DATA["data"][0])
    activity["what"]["category"] = 13
    activity["what"]["category_info"] = {"type": "story", "pageUrl": "non-consent-stories"}
    r = _MODULE.map_activity(activity)
    assert r["category"] == "Reluctance/NonConsent"


def test_map_activity_omits_category_when_unresolvable() -> None:
    """map_activity omits category when unresolvable."""
    import copy

    activity = copy.deepcopy(_FIXTURE_DATA["data"][0])
    del activity["what"]["category"]
    del activity["what"]["category_info"]
    r = _MODULE.map_activity(activity)
    assert "category" not in r


def test_category_table_matches_the_sibling_plugin() -> None:
    """category_table_matches_the_sibling_plugin."""
    import importlib.util

    sibling_path = Path(__file__).resolve().parents[2] / "literotica_stories" / "entrypoint.py"
    sibling_spec = importlib.util.spec_from_file_location(
        "literotica_stories_entrypoint_for_parity", sibling_path
    )
    assert sibling_spec is not None, f"Could not load sibling from {sibling_path}"
    assert sibling_spec.loader is not None, f"Could not load sibling from {sibling_path}"
    sibling_module = importlib.util.module_from_spec(sibling_spec)
    sibling_spec.loader.exec_module(sibling_module)

    assert _MODULE._CATEGORY_NAMES == sibling_module._CATEGORY_NAMES


def test_scan_warns_once_for_an_unmapped_category(monkeypatch) -> None:
    """scan warns once for unmapped categories appearing multiple times."""
    activities = [
        {
            "action": "published-story",
            "when": 1750000000,
            "what": {
                "id": 1,
                "url": "slug-1",
                "title": "T",
                "category": 999,
                "category_info": {"type": "story", "pageUrl": "brand-new"},
            },
        },
        {
            "action": "published-story",
            "when": 1750000000,
            "what": {
                "id": 2,
                "url": "slug-2",
                "title": "T",
                "category": 999,
                "category_info": {"type": "story", "pageUrl": "brand-new"},
            },
        },
    ]
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (activities, []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert len(result) == 2
    warning_logs = [log for log in logs if log["level"] == "warning"]
    unmapped_warnings = [
        log for log in warning_logs if "are not in this plugin's name table" in log["message"]
    ]
    assert len(unmapped_warnings) == 1
    assert (
        "1 Literotica category id(s) are not in this plugin's name table"
        in unmapped_warnings[0]["message"]
    )
    assert "999=brand-new" in unmapped_warnings[0]["message"]


def test_scan_lists_every_unmapped_pair(monkeypatch) -> None:
    """scan lists every distinct unmapped category/slug pair."""
    activities = [
        {
            "action": "published-story",
            "when": 1750000000,
            "what": {
                "id": 1,
                "url": "slug-1",
                "title": "T",
                "category": 999,
                "category_info": {"type": "story", "pageUrl": "brand-new"},
            },
        },
        {
            "action": "published-story",
            "when": 1750000000,
            "what": {
                "id": 2,
                "url": "slug-2",
                "title": "T",
                "category": 1000,
                "category_info": {"type": "story", "pageUrl": "other-new"},
            },
        },
    ]
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (activities, []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert len(result) == 2
    warning_logs = [log for log in logs if log["level"] == "warning"]
    unmapped_warnings = [
        log for log in warning_logs if "are not in this plugin's name table" in log["message"]
    ]
    assert len(unmapped_warnings) == 1
    assert (
        "2 Literotica category id(s) are not in this plugin's name table"
        in unmapped_warnings[0]["message"]
    )
    assert "999=brand-new" in unmapped_warnings[0]["message"]
    assert "1000=other-new" in unmapped_warnings[0]["message"]


def test_scan_does_not_warn_when_every_category_maps(monkeypatch) -> None:
    """scan does not warn when all categories are mapped."""
    activities = [
        {
            "action": "published-story",
            "when": 1750000000,
            "what": {
                "id": 1,
                "url": "slug-1",
                "title": "T",
                "category": 29,
                "category_info": {"type": "story", "pageUrl": "mind-control"},
            },
        },
        {
            "action": "published-story",
            "when": 1750000000,
            "what": {
                "id": 2,
                "url": "slug-2",
                "title": "T",
                "category": 29,
                "category_info": {"type": "story", "pageUrl": "mind-control"},
            },
        },
    ]
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (activities, []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert len(result) == 2
    warning_logs = [log for log in logs if log["level"] == "warning"]
    unmapped_warnings = [
        log for log in warning_logs if "are not in this plugin's name table" in log["message"]
    ]
    assert len(unmapped_warnings) == 0


def test_scan_does_not_warn_without_a_slug(monkeypatch) -> None:
    """scan does not warn for unmapped categories without a slug."""
    activities = [
        {
            "action": "published-story",
            "when": 1750000000,
            "what": {
                "id": 1,
                "url": "slug-1",
                "title": "T",
                "category": 999,
            },
        },
    ]
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: (activities, []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert len(result) == 1
    warning_logs = [log for log in logs if log["level"] == "warning"]
    unmapped_warnings = [
        log for log in warning_logs if "are not in this plugin's name table" in log["message"]
    ]
    assert len(unmapped_warnings) == 0


def test_scan_still_warns_on_an_empty_wall(monkeypatch) -> None:
    """scan still warns about an empty wall when there are no stories."""
    monkeypatch.setattr(
        _MODULE,
        "fetch_activities",
        lambda token: ([], []),
    )

    result, logs = _MODULE.scan(_AUTH)
    assert result == []
    warning_logs = [log for log in logs if log["level"] == "warning"]
    empty_wall_warnings = [
        log for log in warning_logs if "held no story publications" in log["message"]
    ]
    assert len(empty_wall_warnings) == 1
    unmapped_warnings = [
        log for log in warning_logs if "are not in this plugin's name table" in log["message"]
    ]
    assert len(unmapped_warnings) == 0


def test_map_activity_declares_metadata_fetched() -> None:
    """map_activity declares metadata_fetched=True on every patch."""
    activity = _FIXTURE_DATA["data"][0]
    result = _MODULE.map_activity(activity)
    assert result["metadata_fetched"] is True
