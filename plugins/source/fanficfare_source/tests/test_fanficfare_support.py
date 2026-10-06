"""FanFicFare used as a library: the shared support module (LIB-D26)."""

from __future__ import annotations

import logging

import pytest
import requests
from fanficfare import exceptions
from fanficfare_source.fff_support import (
    ConfigurationError,
    _ForwardHandler,
    build_configuration,
    captured_stdout,
    config_sections,
    failure_message,
    is_outage,
    packaged_base_ini,
    quiet_fanficfare_logging,
)

URL = "http://test1.com?sid=1"
STORY = "http://test1.com?sid=1001"
LOGIN_SENTENCE = "the site refused the login — check this site's sign-in in Settings → Credentials"
ADULT_SENTENCE = (
    'the site asks you to confirm you are an adult — turn on "Confirm adult content" in '
    "this plugin's settings"
)
TOTP_SENTENCE = "the site asks for a one-time password, which a background download cannot give"


def test_quiet_logging_leaves_one_forwarding_handler_at_warning() -> None:
    """Call quiet_fanficfare_logging twice and verify idempotence."""
    quiet_fanficfare_logging()
    quiet_fanficfare_logging()
    log = logging.getLogger("fanficfare")
    assert log.level == logging.WARNING
    assert log.propagate is False
    assert len(log.handlers) == 1
    assert isinstance(log.handlers[0], _ForwardHandler)


def test_a_fanficfare_warning_is_relogged_through_the_plugin_logger(caplog) -> None:  # type: ignore[no-untyped-def]
    """Verify FanFicFare warnings are re-logged through the plugin logger."""
    quiet_fanficfare_logging()
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.fff_support"):
        logging.getLogger("fanficfare.adapters").warning("site slow")
        logging.getLogger("fanficfare.adapters").debug("noise")
    assert [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == "fanficfare_source.fff_support"
    ] == [(logging.WARNING, "FanFicFare: site slow")]


def test_nothing_fanficfare_logs_reaches_stderr(capsys) -> None:  # type: ignore[no-untyped-def]
    """Verify that FanFicFare logs do not reach stderr."""
    quiet_fanficfare_logging()
    capsys.readouterr()
    logging.getLogger("fanficfare").warning("w")
    logging.getLogger("fanficfare").debug("d")
    assert capsys.readouterr().err == ""


def test_captured_stdout_keeps_prints_off_stdout_and_logs_them_at_debug(capsys, caplog) -> None:  # type: ignore[no-untyped-def]
    """Verify that prints are captured and logged at DEBUG level."""
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.fff_support"), captured_stdout():
        print("hello")  # noqa: T201
    assert capsys.readouterr().out == ""
    assert "FanFicFare printed 5 character(s): hello" in [r.getMessage() for r in caplog.records]
    assert any(
        r.levelno == logging.DEBUG and "FanFicFare printed" in r.getMessage()
        for r in caplog.records
    )


def test_captured_stdout_logs_nothing_when_nothing_was_printed(caplog) -> None:  # type: ignore[no-untyped-def]
    """Verify that empty output produces no log."""
    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.fff_support"), captured_stdout():
        pass
    assert not any(r.getMessage().startswith("FanFicFare printed") for r in caplog.records)


def test_config_sections_of_an_unknown_story_address_raise() -> None:
    """Verify that unknown addresses raise when unknown_site_ok=False."""
    with pytest.raises(exceptions.UnknownSite):
        config_sections("https://example.org/x", unknown_site_ok=False)


def test_config_sections_of_an_unknown_listing_are_unknown() -> None:
    """Verify that unknown listings return ['unknown'] when unknown_site_ok=True."""
    assert config_sections("https://example.org/x", unknown_site_ok=True) == ["unknown"]


def test_config_sections_of_a_known_site() -> None:
    """Verify that known sites return their section names."""
    assert "test1.com" in config_sections(STORY, unknown_site_ok=False)


def test_build_configuration_applies_overrides_last() -> None:
    """Verify that overrides win over every layer beneath them."""
    sections = config_sections(STORY, unknown_site_ok=False)
    plain = build_configuration(
        sections, fileform="epub", base_ini=packaged_base_ini(), options={}, extra_options=""
    )
    assert plain.getConfig("is_adult") is False
    overridden = build_configuration(
        sections,
        fileform="epub",
        base_ini=packaged_base_ini(),
        options={},
        extra_options="",
        overrides={"is_adult": "true"},
    )
    assert overridden.getConfig("is_adult") == "true"


def test_broken_advanced_options_never_leak_their_text(caplog) -> None:  # type: ignore[no-untyped-def]
    """Verify that ConfigurationError doesn't leak a password from the advanced options."""
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(
            ConfigurationError, match=r"^Could not read the Advanced FanFicFare options: \w+$"
        ) as info,
    ):
        build_configuration(
            config_sections(STORY, unknown_site_ok=False),
            fileform="epub",
            base_ini=packaged_base_ini(),
            options={},
            extra_options="[defaults]\npassword sekrit\n",
        )
    assert "sekrit" not in str(info.value)
    assert "sekrit" not in caplog.text
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            exceptions.UnknownSite(URL, []),
            "unsupported site: FanFicFare does not support http://test1.com?sid=1",
        ),
        (exceptions.StoryDoesNotExist(URL), "story not found: http://test1.com?sid=1"),
        (exceptions.AccessDenied("x"), "the site refused access to this story"),
        (exceptions.FailedToLogin(URL, "me"), LOGIN_SENTENCE),
        (exceptions.AdultCheckRequired(URL), ADULT_SENTENCE),
        (exceptions.NeedTimedOneTimePassword(URL), TOTP_SENTENCE),
        (
            exceptions.InvalidStoryURL(URL, "test1.com", "x"),
            "not a story address FanFicFare recognises: http://test1.com?sid=1",
        ),
        (exceptions.HTTPErrorFFF(URL, 503, "Service Unavailable"), "the site answered HTTP 503"),
        (
            exceptions.FailedToDownload("Error downloading Chapter: x!"),
            "download failed: Error downloading Chapter: x!",
        ),
        (requests.exceptions.ConnectionError(), "the site could not be reached"),
        (requests.exceptions.ReadTimeout(), "the site could not be reached"),
        (
            ConfigurationError("Could not parse FanFicFare configuration: KeyError"),
            "Could not parse FanFicFare configuration: KeyError",
        ),
        (ValueError("secret text"), "FanFicFare failed (ValueError)"),
    ],
)
def test_failure_messages(exc: BaseException, expected: str) -> None:
    """Verify failure_message produces correct user-facing sentences."""
    assert failure_message(exc, URL) == expected


@pytest.mark.pins("EXP-169")
def test_a_failure_message_never_carries_a_dotted_class_path() -> None:
    """Verify that failure messages don't expose internal exception class paths."""
    exceptions_to_test = [
        exceptions.UnknownSite(URL, []),
        exceptions.StoryDoesNotExist(URL),
        exceptions.AccessDenied("x"),
        exceptions.FailedToLogin(URL, "me"),
        exceptions.AdultCheckRequired(URL),
        exceptions.NeedTimedOneTimePassword(URL),
        exceptions.InvalidStoryURL(URL, "test1.com", "x"),
        exceptions.HTTPErrorFFF(URL, 503, "Service Unavailable"),
        exceptions.FailedToDownload("Error downloading Chapter: x!"),
    ]
    for exc in exceptions_to_test:
        assert "fanficfare.exceptions." not in failure_message(exc, URL)
    assert "secret text" not in failure_message(ValueError("secret text"), URL)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (exceptions.HTTPErrorFFF(URL, 429, "x"), True),
        (exceptions.HTTPErrorFFF(URL, 500, "x"), True),
        (exceptions.HTTPErrorFFF(URL, 503, "x"), True),
        (exceptions.HTTPErrorFFF(URL, 404, "x"), False),
        (exceptions.HTTPErrorFFF(URL, 403, "x"), False),
        (requests.exceptions.ConnectionError(), True),
        (requests.exceptions.ReadTimeout(), True),
        (exceptions.StoryDoesNotExist(URL), False),
        (exceptions.FailedToLogin(URL, "me"), False),
    ],
)
def test_is_outage(exc: BaseException, expected: bool) -> None:
    """Verify is_outage correctly identifies site outages."""
    assert is_outage(exc) == expected
