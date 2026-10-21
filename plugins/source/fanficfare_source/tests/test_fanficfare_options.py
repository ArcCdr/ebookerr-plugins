"""Plugin settings as FanFicFare options and advanced option filtering (C35–C37)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from ebookerr_sdk.spi import SiteCredential
from ebookerr_sdk.testing import make_request, run_wire
from fanficfare.configurable import Configuration
from fanficfare_source.fff_support import (
    apply_sign_in,
    build_configuration,
    config_sections,
    packaged_base_ini,
    settings_options,
    strip_naming_keys,
)
from fanficfare_source.library import FanFicFareLibraryGateway
from fanficfare_source.plugin import FanFicFareSourcePlugin

STORY = "http://test1.com?sid=1001"


def test_the_packaged_base_ini_is_the_c35_text() -> None:
    """The packaged base.ini holds the exact C35 text."""
    expected = "[epub]\nadd_to_replace_metadata:\n oneshot=>Completed=>Completed\\,Oneshot&&numChapters=>^1$\n"  # noqa: E501 — C35 spec is longer than 100 chars
    assert packaged_base_ini() == expected


def test_settings_become_options_by_section() -> None:
    """Settings map to the correct ini section and render as option text."""
    settings = {
        "is_adult": False,
        "include_images": False,
        "include_subject_tags": "genre, status",
        "keep_summary_html": True,
        "make_firstimage_cover": True,
        "extra_options": "x",
    }
    result = settings_options(settings)
    assert result == {
        "defaults": {"is_adult": "false", "include_subject_tags": "genre, status"},
        "epub": {
            "include_images": "false",
            "keep_summary_html": "true",
            "make_firstimage_cover": "true",
        },
    }


def test_only_present_settings_become_options() -> None:
    """Only settings present in the input appear in the output."""
    result = settings_options({"is_adult": True})
    assert result == {"defaults": {"is_adult": "true"}, "epub": {}}


def test_a_percent_in_a_setting_is_doubled() -> None:
    """Percent signs are doubled for FanFicFare's ini interpolation."""
    result = settings_options({"include_subject_tags": "a%b"})
    assert result["defaults"]["include_subject_tags"] == "a%%b"


def test_strip_naming_keys_drops_every_naming_line(caplog: Any) -> None:
    """All naming-related option lines are removed, along with their continuations."""
    text = (
        "[defaults]\noutput_filename: x/${title}\nis_adult: true\n[www.example.com]\n"
        "make_directories: true\nzip_filename: a\n  continued\nkeep: 1"
    )
    expected = "[defaults]\nis_adult: true\n[www.example.com]\nkeep: 1"
    result = strip_naming_keys(text)
    assert result == expected
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert any("'output_filename' is ignored" in w for w in warnings)
    assert any("'make_directories' is ignored" in w for w in warnings)
    assert any("'zip_filename' is ignored" in w for w in warnings)


def test_a_repeated_naming_key_warns_once_per_call(caplog: Any) -> None:
    """The same naming key appears in multiple places but only warned once."""
    text = "[a]\noutput_filename: x\n[b]\noutput_filename: y"
    strip_naming_keys(text)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "'output_filename' is ignored" in warnings[0]


def test_a_basic_sign_in_sets_username_and_password(caplog: Any) -> None:
    """A basic credential is written to the config's overrides section."""
    config = Configuration(["test1.com"], "epub")
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.fff_support"):
        apply_sign_in(config, SiteCredential("basic", "me", "pw"), "test1.com")
    assert config.getConfig("username") == "me"
    assert config.getConfig("password") == "pw"
    debug_logs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and r.name == "fanficfare_source.fff_support"
    ]
    assert any("Using the stored sign-in for test1.com" in m for m in debug_logs)


def test_a_password_with_a_percent_is_escaped() -> None:
    """Percent signs in passwords are escaped for the ini parser."""
    config = Configuration(["test1.com"], "epub")
    apply_sign_in(config, SiteCredential("basic", "me", "50%off"), "test1.com")
    assert config.getConfig("password") == "50%off"
    assert config.get("overrides", "password", raw=True) == "50%%off"


def test_another_sign_in_kind_is_not_used_with_a_warning(caplog: Any) -> None:
    """Non-basic credentials are not used and generate exactly the C37 warning."""
    config = Configuration(["test1.com"], "epub")
    apply_sign_in(config, SiteCredential("cookie", "sid", "v"), "test1.com")
    assert config.getConfig("username") == ""
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "fanficfare_source.fff_support"
    ]
    assert warnings == [
        "FanFicFare uses only a username and password; "
        "the cookie sign-in stored for test1.com is not used"
    ]


def test_no_sign_in_sets_nothing(caplog: Any) -> None:
    """None credentials set nothing and produce no log output."""
    config = Configuration(["test1.com"], "epub")
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.fff_support"):
        apply_sign_in(config, None, "test1.com")
    assert not config.has_option("overrides", "username")
    log_records = [
        r
        for r in caplog.records
        if r.levelno in (logging.DEBUG, logging.WARNING)
        and r.name == "fanficfare_source.fff_support"
    ]
    assert len(log_records) == 0


def test_settings_reach_the_configuration() -> None:
    """The plugin's settings arrive in the story's FanFicFare configuration (C36)."""
    gateway = FanFicFareLibraryGateway({"is_adult": False, "include_images": False})
    config = gateway._configuration(STORY)
    assert config.getConfig("is_adult") is False
    assert config.getConfig("include_images") is False


@pytest.mark.pins("EXP-381")
def test_a_card_control_wins_over_the_same_option_in_the_advanced_text() -> None:
    """The card's settings win over the advanced options, a site's section included (LIB-D47)."""
    sections = config_sections(STORY, unknown_site_ok=False)
    base_ini = "[defaults]\ninclude_subject_tags: base\nkeep_summary_html: base\n"
    options = {"defaults": {"include_subject_tags": "settings"}}
    advanced = build_configuration(
        sections,
        fileform="epub",
        base_ini=base_ini,
        options=options,
        extra_options=(
            "[defaults]\ninclude_subject_tags: advanced\nkeep_summary_html: advanced\n"
            "[test1.com]\ninclude_subject_tags: site\n"
        ),
    )
    assert advanced.getConfig("include_subject_tags") == "settings"
    assert advanced.getConfig("keep_summary_html") == "advanced"
    settings_only = build_configuration(
        sections, fileform="epub", base_ini=base_ini, options=options, extra_options=""
    )
    assert settings_only.getConfig("include_subject_tags") == "settings"
    base_only = build_configuration(
        sections, fileform="epub", base_ini=base_ini, options={}, extra_options=""
    )
    assert base_only.getConfig("include_subject_tags") == "base"


def test_the_adult_switch_wins_over_is_adult_in_the_advanced_text() -> None:
    """With *Confirm adult content* on, ``is_adult: false`` in the text changes nothing."""
    gateway = FanFicFareLibraryGateway(
        {"is_adult": True, "extra_options": "[test1.com]\nis_adult: false\n"}
    )
    # FanFicFare hands back the text "true"; only "false" becomes a bool
    assert gateway._configuration(STORY).getConfig("is_adult") == "true"


def test_a_naming_key_in_the_advanced_options_is_ignored_with_a_warning(caplog: Any) -> None:
    """A file-naming option in the advanced options is dropped; one WARNING names it (D56, C37)."""
    gateway = FanFicFareLibraryGateway(
        {"extra_options": "[defaults]\noutput_filename: x/${title}\n"}
    )
    config = gateway._configuration(STORY)
    assert config.getConfig("output_filename") == "${title}${formatext}"
    assert config.getConfig("make_directories") is False
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "fanficfare_source.fff_support"
    ]
    assert len(warnings) == 1
    assert "'output_filename' is ignored" in warnings[0]


def test_a_basic_sign_in_reaches_fanficfare() -> None:
    """The site's stored basic sign-in becomes FanFicFare's username and password (C36)."""
    gateway = FanFicFareLibraryGateway(
        {}, credentials=lambda url: SiteCredential("basic", "me", "pw")
    )
    config = gateway._configuration(STORY)
    assert config.getConfig("username") == "me"
    assert config.getConfig("password") == "pw"


def _check(text: str) -> list[dict[str, Any]]:
    """Ask the plugin's save check about *text*, over the wire as the core asks it (FR-CFG-4)."""
    request = make_request("validate_settings", settings={"is_adult": True, "extra_options": text})
    terminal, _frames = run_wire(FanFicFareSourcePlugin(), request)
    assert terminal["ok"] is True, terminal.get("error")
    return terminal["result"]


@pytest.mark.pins("EXP-380")
def test_advanced_options_with_a_parse_error_or_a_sign_in_are_refused_at_save() -> None:
    """An unreadable line is refused by its number, a sign-in line with a link; neither quoted."""
    unreadable = _check("[defaults]\nslow_down_sleep_time: 2\nthis line has no separator")
    sign_in = _check(
        "[www.example.com]\nusername: reader\npassword: hunter2\nslow_down_sleep_time: 2"
    )

    assert unreadable == [
        {
            "field": "extra_options",
            "message": "line 3: FanFicFare cannot read this line — write an option as name: value",
            "blocking": True,
            "href": None,
        }
    ]
    assert sign_in == [
        {
            "field": "extra_options",
            "message": (
                "lines 2 and 3: sign-ins belong in Settings → Credentials, not in these options"
            ),
            "blocking": True,
            "href": "/settings/credentials",
        }
    ]


def test_valid_advanced_options_pass_the_save_check() -> None:
    """Options FanFicFare reads, comments and a file-naming key included, raise nothing."""
    text = "# mine\n[defaults]\nslow_down_sleep_time: 2\noutput_filename: x\n[www.example.com]\n"
    assert _check(text) == []
    assert _check("") == []


def test_an_option_above_every_section_is_refused_by_its_line() -> None:
    """A line before the first section line is refused with what it lacks (EXP-380 repro a)."""
    (problem,) = _check("slow_down_sleep_time = 2\n[www.literotica.com\nfoo")
    assert problem["message"] == (
        "line 1: an option needs a section line above it, such as [defaults]"
    )


def test_a_sign_in_key_is_refused_in_any_section_and_any_case() -> None:
    """``Password`` under ``[defaults]`` and ``cookie`` under ``[overrides]`` are sign-ins too."""
    (problem,) = _check("[defaults]\nPassword: x\n[overrides]\ncookie = y\nkeep: 1")
    assert problem["message"] == (
        "lines 2 and 4: sign-ins belong in Settings → Credentials, not in these options"
    )


def test_the_manifest_declares_the_save_check() -> None:
    """The manifest declares its validator and SPI 3.10, the interface carrying it (FR-CFG-4)."""
    manifest = FanFicFareSourcePlugin.manifest
    assert manifest.settings_schema.validator is True
    assert manifest.spi_version == "3.10"


def test_an_option_the_card_sets_is_a_warning_at_save() -> None:
    """A line setting what a card control sets saves, with a warning naming that control."""
    text = "[defaults]\nis_adult: false\nslow_down_sleep_time: 2\n[test1.com]\ninclude_images: no"
    assert _check(text) == [
        {
            "field": "extra_options",
            "message": (
                'lines 2 and 5: is_adult and include_images are set by "Confirm adult content" '
                'and "Include images" on this card, which wins'
            ),
            "blocking": False,
            "href": None,
        }
    ]
