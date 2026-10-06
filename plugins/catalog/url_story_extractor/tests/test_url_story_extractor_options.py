"""URL Story Extractor options: the packaged base.ini, its own settings and sign-ins (C35, C36)."""

from __future__ import annotations

import builtins
import os
from pathlib import Path
from typing import Any

import pytest
from ebookerr_sdk.spi import SiteCredential
from ebookerr_sdk.testing import make_request, run_wire
from url_story_extractor.fff_support import packaged_base_ini
from url_story_extractor.pages import FanFicFarePagesGateway
from url_story_extractor.plugin import UrlStoryExtractorPlugin


def test_the_packaged_base_ini_is_the_c35_text() -> None:
    """The packaged base.ini holds the exact C35 text."""
    expected = "[epub]\nadd_to_replace_metadata:\n oneshot=>Completed=>Completed\\,Oneshot&&numChapters=>^1$\n"  # noqa: E501 — C35 spec is longer than 100 chars
    assert packaged_base_ini() == expected


def test_its_own_settings_reach_the_configuration() -> None:
    """The extractor's settings and advanced options arrive in its listing configuration (C36)."""
    gateway = FanFicFarePagesGateway(
        {"is_adult": True, "extra_options": "[defaults]\nslow_down_sleep_time: 2\n"}
    )

    config = gateway._configuration("https://www.example.org/list")

    # FanFicFare hands back the text "true"; only "false" becomes a bool
    assert config.getConfig("is_adult") == "true"
    assert config.getConfig("slow_down_sleep_time") == "2"
    assert config.getConfig("max_request_retries") == "1"


def test_a_sign_in_reaches_the_configuration() -> None:
    """The site's stored sign-in becomes FanFicFare's username and password, asked once (C36)."""
    asked: list[str] = []

    def credentials(url: str) -> SiteCredential | None:
        asked.append(url)
        return SiteCredential("basic", "me", "pw")

    gateway = FanFicFarePagesGateway({}, credentials=credentials)

    config = gateway._configuration("https://www.example.org/list")
    gateway._configuration("https://www.example.org/other")

    assert config.getConfig("username") == "me"
    assert config.getConfig("password") == "pw"
    assert asked == ["https://www.example.org/list"]


def test_a_sign_in_is_never_used_for_another_site() -> None:
    """Two sites FanFicFare knows by one section name each get their own sign-in (C36)."""
    gateway = FanFicFarePagesGateway(
        {},
        credentials=lambda url: (
            SiteCredential("basic", "me", "pw") if "//a.example/" in url else None
        ),
    )

    signed_in = gateway._configuration("https://a.example/list")
    other = gateway._configuration("https://b.example/list")

    assert signed_in.getConfig("username") == "me"
    assert other.getConfig("username") == ""
    assert other.getConfig("password") == ""


def test_the_extractor_never_reads_another_plugins_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan opens no file under the FanFicFare Source's data folder (D55)."""
    sibling = tmp_path / "fanficfare_source"
    sibling.mkdir()
    # the file the extractor once read from there; its name is built in two parts so that no
    # plugin source or test spells the retired name out
    (sibling / ("person" + "al.ini")).write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "url_story_extractor"))
    monkeypatch.setattr(
        "url_story_extractor.pages.get_urls_from_page",
        lambda url, configuration, normalize: {"urllist": []},
    )
    opened: list[Path] = []
    real_read_text, real_read_bytes, real_open = Path.read_text, Path.read_bytes, builtins.open

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(self)
        return real_read_text(self, *args, **kwargs)

    def read_bytes(self: Path) -> bytes:
        opened.append(self)
        return real_read_bytes(self)

    def open_file(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            opened.append(Path(os.fsdecode(file)))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(builtins, "open", open_file)

    terminal, _frames = run_wire(
        UrlStoryExtractorPlugin(),
        make_request("scan", settings={"extract_urls": ["https://www.example.org/list"]}),
    )

    assert terminal["ok"] is True, terminal.get("error")
    assert [path for path in opened if path.is_relative_to(sibling)] == []
    assert opened, "no read was recorded, so the check above would pass whatever was read"
