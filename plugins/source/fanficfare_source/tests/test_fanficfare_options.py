"""Plugin settings as FanFicFare options and advanced option filtering (C35–C37)."""

from __future__ import annotations

import logging
from typing import Any

from ebookerr_sdk.spi import SiteCredential
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
    """Non-basic credentials are not used and generate a warning."""
    config = Configuration(["test1.com"], "epub")
    apply_sign_in(config, SiteCredential("cookie", "sid", "v"), "test1.com")
    assert config.getConfig("username") == ""
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("FanFicFare uses only a username and password" in w for w in warnings)
    assert any("cookie sign-in for test1.com is not used" in w for w in warnings)


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


def test_advanced_options_are_applied_after_the_settings() -> None:
    """The advanced options win over the settings, which win over the base options (C36)."""
    sections = config_sections(STORY, unknown_site_ok=False)
    base_ini = "[defaults]\ninclude_subject_tags: base\n"
    options = {"defaults": {"include_subject_tags": "settings"}}
    advanced = build_configuration(
        sections,
        fileform="epub",
        base_ini=base_ini,
        options=options,
        extra_options="[defaults]\ninclude_subject_tags: advanced\n",
    )
    assert advanced.getConfig("include_subject_tags") == "advanced"
    settings_only = build_configuration(
        sections, fileform="epub", base_ini=base_ini, options=options, extra_options=""
    )
    assert settings_only.getConfig("include_subject_tags") == "settings"
    base_only = build_configuration(
        sections, fileform="epub", base_ini=base_ini, options={}, extra_options=""
    )
    assert base_only.getConfig("include_subject_tags") == "base"


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
