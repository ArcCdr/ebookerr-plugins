"""FanFicFare is routed past a browser check through its own options (FR-CHS-10)."""

from __future__ import annotations

import email.message
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

import pytest
from ebookerr_sdk.challenge import ChallengeRouter, Clearance, ClearanceCookie
from ebookerr_sdk.testing import FakeContext, FakeSolver
from fanficfare import exceptions
from fanficfare_source.fff_support import (
    build_configuration,
    challenge_outcome,
    challenge_route,
    packaged_base_ini,
    seed_clearance,
    solver_options,
)

URL = "https://example.org/story/1"


def _check(url: str) -> urllib.error.HTTPError:
    """A urllib browser-check answer for *url*."""
    msg = email.message.Message()
    msg["cf-mitigated"] = "challenge"
    return urllib.error.HTTPError(url, 403, "Forbidden", msg, None)


def _config(overrides: dict[str, str]) -> Any:
    """A FanFicFare configuration for an unknown site with *overrides*."""
    return build_configuration(
        ["unknown"],
        fileform="epub",
        base_ini=packaged_base_ini(),
        options={},
        extra_options="",
        overrides=overrides,
    )


def test_a_host_checked_on_every_page_sends_fanficfare_through_the_solver() -> None:
    """The every-page mark makes FanFicFare post the page to the solver."""
    with FakeSolver(body="<html><body><p>through the solver</p></body></html>") as fake:
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        ctx.state_set("challenge.every_page:www.example.org", True)
        options, clearance = challenge_route(ChallengeRouter(ctx), "www.example.org")
        assert clearance is None
        assert fake.health_calls == 1
        assert options == {
            "use_flaresolverr_proxy": "directimages",
            "flaresolverr_proxy_protocol": "http",
            "flaresolverr_proxy_address": "127.0.0.1",
            "flaresolverr_proxy_port": str(int(fake.url.rsplit(":", 1)[1])),
            "flaresolverr_proxy_timeout": "60000",
        }
        config = _config(options)
        content, _ = config.get_fetcher().get_request_redirected("https://www.example.org/story/1")
        assert "through the solver" in content
        assert fake.requests[0]["cmd"] == "request.get"
        assert fake.requests[0]["url"] == "https://www.example.org/story/1"
        assert fake.requests[0]["maxTimeout"] == 60000


def test_a_kept_clearance_reaches_the_site_through_fanficfare() -> None:
    """The kept User-Agent and cookies are what the site sees."""
    seen: list[tuple[str | None, str | None]] = []

    class _Handler(BaseHTTPRequestHandler):
        """Record each GET's User-Agent and Cookie."""

        def log_message(self, *args: object) -> None:
            """Stay quiet."""

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            """Answer a small page."""
            seen.append((self.headers.get("User-Agent"), self.headers.get("Cookie")))
            body = b"<html><body>ok</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        site_url = f"http://127.0.0.1:{server.server_address[1]}/story/1"
        ctx = FakeContext()
        kept = Clearance("UA-Y", (ClearanceCookie("clearance", "v1", "127.0.0.1"),))
        ctx.state_set("challenge.clearance:127.0.0.1", kept.to_state())
        options, clearance = challenge_route(ChallengeRouter(ctx), "127.0.0.1")
        assert (options, clearance) == ({"user_agent": "UA-Y"}, kept)
        config = _config(options)
        seed_clearance(config, kept, site_url)
        config.get_fetcher().get_request_redirected(site_url)
    finally:
        server.shutdown()
        server.server_close()
    assert seen[0] == ("UA-Y", "clearance=v1")


@pytest.mark.parametrize(
    ("address", "protocol", "host", "port"),
    [
        ("http://192.168.1.50:8191", "http", "192.168.1.50", "8191"),
        ("https://solver.example.test", "https", "solver.example.test", "443"),
        ("http://[fd00::5]:8191/x", "http", "[fd00::5]", "8191"),
    ],
)
def test_solver_options_read_the_address(address: str, protocol: str, host: str, port: str) -> None:
    """The protocol, host and port come from the solver's address."""
    options = solver_options(address)
    assert options["flaresolverr_proxy_protocol"] == protocol
    assert options["flaresolverr_proxy_address"] == host
    assert options["flaresolverr_proxy_port"] == port


def test_a_percent_in_the_user_agent_is_escaped() -> None:
    """A % in the kept User-Agent is doubled and read back whole."""
    ctx = FakeContext()
    kept = Clearance("Mozilla/5.0 (50%)", (ClearanceCookie("c", "v", "example.org"),))
    ctx.state_set("challenge.clearance:example.org", kept.to_state())
    options, _ = challenge_route(ChallengeRouter(ctx), "example.org")
    assert options["user_agent"] == "Mozilla/5.0 (50%%)"
    assert _config(options).getConfig("user_agent") == "Mozilla/5.0 (50%)"


def test_a_403_is_probed_and_retried_once_the_host_is_routed() -> None:
    """A 403 on a host not yet routed is probed; the passed check says to run again."""
    ok = mock.MagicMock()
    ok.__enter__.return_value = ok
    ok.status = 200
    ok.headers = email.message.Message()
    ok.read.return_value = b"<html></html>"
    with FakeSolver() as fake:
        router = ChallengeRouter(FakeContext(app_settings={"challenge_solver_url": fake.url}))
        with mock.patch("urllib.request.urlopen", side_effect=[_check(URL), ok]):
            assert challenge_outcome(exceptions.HTTPErrorFFF(URL, 403, "Forbidden"), URL, router) is True


def test_a_403_without_a_solver_reads_the_no_solver_sentence() -> None:
    """No solver set: the user reads the set-a-solver sentence."""
    router = ChallengeRouter(FakeContext())
    with mock.patch("urllib.request.urlopen", side_effect=_check(URL)):
        outcome = challenge_outcome(exceptions.HTTPErrorFFF(URL, 403, "Forbidden"), URL, router)
    assert outcome == "example.org asks for a browser check — set a challenge solver in Settings → General"


def test_a_host_checked_on_every_page_that_still_fails_reads_could_not_pass() -> None:
    """A 428 from a routed host means the solver could not pass."""
    with FakeSolver() as fake:
        ctx = FakeContext(app_settings={"challenge_solver_url": fake.url})
        ctx.state_set("challenge.every_page:example.org", True)
        outcome = challenge_outcome(
            exceptions.HTTPErrorFFF(URL, 428, "Precondition Required"), URL, ChallengeRouter(ctx)
        )
    assert outcome == "example.org asks for a browser check that the challenge solver could not pass"


@pytest.mark.parametrize(
    ("exc", "with_router"),
    [
        (exceptions.HTTPErrorFFF(URL, 404, "Not Found"), True),
        (exceptions.FailedToDownload("x"), True),
        (exceptions.HTTPErrorFFF(URL, 403, "Forbidden"), False),
    ],
)
def test_other_failures_keep_their_reason(exc: BaseException, with_router: bool) -> None:
    """Anything but a 403/428 HTTP failure with a router is left as it is."""
    router = ChallengeRouter(FakeContext()) if with_router else None
    with mock.patch("urllib.request.urlopen") as urlopen:
        assert challenge_outcome(exc, URL, router) is False
        urlopen.assert_not_called()
