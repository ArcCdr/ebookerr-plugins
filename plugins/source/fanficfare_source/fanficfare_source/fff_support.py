"""FanFicFare used as a library: one configuration, quiet logs, a clean stdout, plain errors.

Every first-party plugin that imports FanFicFare carries this file byte for byte — a plugin may
import only its own package — and the plugins repository's
``tests/test_fanficfare_support_twins.py`` fails when two copies differ (``LIB-D26``). It gives its
plugin four things:

* :func:`config_sections` and :func:`build_configuration` — a FanFicFare ``Configuration`` layered
  from FanFicFare's own ``defaults.ini``, the install's ``personal.ini`` and per-call overrides; a
  parse error never quotes a line of ``personal.ini``, which may hold a site password;
* :func:`quiet_fanficfare_logging` — importing ``fanficfare`` attaches a DEBUG handler that writes
  to stderr, which the core reports as a WARNING after the plugin's run; once quieted, only
  FanFicFare's WARNING and ERROR records reach the log, re-logged through this plugin's logger;
* :func:`captured_stdout` — the plugin process's stdout carries the core's wire protocol, so what
  FanFicFare prints is captured and logged at DEBUG instead;
* :func:`failure_message` and :func:`is_outage` — one plain sentence per FanFicFare failure, and
  whether it means the site was unreachable.
"""

from __future__ import annotations

import contextlib
import io
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType

import fanficfare
import requests
from fanficfare import adapters, exceptions
from fanficfare.configurable import Configuration

logger = logging.getLogger(__name__)

FANFICFARE_LOGGER = "fanficfare"
"""The logger every FanFicFare module logs under."""

_PRINTED_MAX_CHARS = 2000
"""At most this many printed characters reach the DEBUG line."""

_NO_OVERRIDES: Mapping[str, str] = MappingProxyType({})
"""The default: no per-call option."""


class ConfigurationError(RuntimeError):
    """``personal.ini`` could not be parsed; the message names only the exception type."""


class _ForwardHandler(logging.Handler):
    """Re-log a FanFicFare record through this module's logger as ``FanFicFare: <message>``."""

    def emit(self, record: logging.LogRecord) -> None:
        """Re-log *record* at its own level.

        Args:
            record: A FanFicFare log record (WARNING or above).
        """
        logger.log(record.levelno, "FanFicFare: %s", record.getMessage())


def quiet_fanficfare_logging() -> None:
    """Let only FanFicFare's WARNING and ERROR records reach the log, through this plugin's logger.

    Idempotent. Call it after ``import fanficfare`` has run — the import attaches a DEBUG
    ``StreamHandler`` writing to stderr and sets the ``fanficfare`` logger to DEBUG, which undoes an
    earlier call. Removes every handler on that logger, sets it to WARNING, stops propagation and
    attaches one :class:`_ForwardHandler`.
    """
    fff_logger = logging.getLogger(FANFICFARE_LOGGER)
    for handler in list(fff_logger.handlers):
        fff_logger.removeHandler(handler)
    fff_logger.setLevel(logging.WARNING)
    fff_logger.propagate = False
    fff_logger.addHandler(_ForwardHandler())


def config_sections(url: str, *, unknown_site_ok: bool) -> list[str]:
    """Return FanFicFare's configuration sections for *url*.

    Args:
        url: A story or listing page address.
        unknown_site_ok: ``True`` for a page any site may serve (a listing): an address no
            FanFicFare adapter claims gets the ``unknown`` section. ``False`` for a story: the
            error propagates.

    Returns:
        The section names, in FanFicFare's order.

    Raises:
        Exception: FanFicFare's own error (``UnknownSite`` for an address no adapter claims) when
            *unknown_site_ok* is ``False``.
    """
    try:
        return list(adapters.getConfigSectionsFor(url))
    except Exception:  # noqa: BLE001 — FanFicFare raises several types here
        if not unknown_site_ok:
            raise
        return ["unknown"]


def build_configuration(
    sections: list[str],
    personal_ini: Path,
    *,
    fileform: str,
    overrides: Mapping[str, str] = _NO_OVERRIDES,
    lightweight: bool = False,
) -> Configuration:
    """Build a FanFicFare configuration: ``defaults.ini``, then *personal_ini*, then *overrides*.

    Args:
        sections: What :func:`config_sections` returned for the address.
        personal_ini: The install's FanFicFare ``personal.ini``.
        fileform: The output format FanFicFare's per-format sections are keyed by.
        overrides: Option name to value, set in FanFicFare's ``overrides`` section, which wins over
            every file.
        lightweight: FanFicFare's lightweight mode (no output-format machinery).

    Returns:
        The configuration.

    Raises:
        ConfigurationError: *personal_ini* could not be parsed. The message carries only the
            exception's type name and the original is not chained — a parse error quotes the
            offending line, which may be a site login.
    """
    config = Configuration(sections, fileform, lightweight=lightweight)
    defaults_ini = Path(fanficfare.__file__).parent / "defaults.ini"
    try:
        config.read([str(defaults_ini), str(personal_ini)])
    except Exception as exc:  # noqa: BLE001 — none may leak its text
        raise ConfigurationError(
            f"Could not parse FanFicFare configuration: {type(exc).__name__}"
        ) from None
    if not config.has_section("overrides"):
        config.add_section("overrides")
    for key, value in overrides.items():
        config.set("overrides", key, value)
    return config


@contextlib.contextmanager
def captured_stdout() -> Iterator[None]:
    """Run the block with ``sys.stdout`` captured, then log whatever was printed at DEBUG.

    The plugin process's stdout is the core's wire. The SDK host writes its frames through its own
    reference to the original stream, so this redirect never reaches them.
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            yield
    finally:
        printed = buffer.getvalue().strip()
        if printed:
            logger.debug(
                "FanFicFare printed %d character(s): %s",
                len(printed),
                printed[:_PRINTED_MAX_CHARS],
            )


def failure_message(exc: BaseException, url: str) -> str:  # noqa: C901
    """Return one plain, user-facing sentence for a FanFicFare failure on *url* (``LIB-D25``).

    Args:
        exc: What FanFicFare, or its HTTP client, raised.
        url: The address it was working on.

    Returns:
        The sentence. An exception this table does not know names only its type, never its text.
    """
    if isinstance(exc, exceptions.UnknownSite):
        return f"unsupported site: FanFicFare does not support {url}"
    if isinstance(exc, exceptions.StoryDoesNotExist):
        return f"story not found: {url}"
    if isinstance(exc, exceptions.AccessDenied):
        return "the site refused access to this story"
    if isinstance(exc, exceptions.FailedToLogin):
        return (
            "the site refused the login — check this site's username and password in "
            "FanFicFare's personal.ini"
        )
    if isinstance(exc, exceptions.AdultCheckRequired):
        return (
            "the site asks you to confirm you are an adult — set is_adult:true in "
            "FanFicFare's personal.ini"
        )
    if isinstance(exc, exceptions.NeedTimedOneTimePassword):
        return "the site asks for a one-time password, which a background download cannot give"
    if isinstance(exc, exceptions.InvalidStoryURL):
        return f"not a story address FanFicFare recognises: {url}"
    if isinstance(exc, exceptions.HTTPErrorFFF):
        return f"the site answered HTTP {exc.status_code}"
    if isinstance(exc, exceptions.FailedToDownload):
        return f"download failed: {exc}"
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "the site could not be reached"
    if isinstance(exc, ConfigurationError):
        return str(exc)
    return f"FanFicFare failed ({type(exc).__name__})"


def is_outage(exc: BaseException) -> bool:
    """Whether *exc* means the site was unreachable or overloaded — what its breaker counts.

    Args:
        exc: What FanFicFare, or its HTTP client, raised.

    Returns:
        ``True`` for HTTP 429 or 5xx and for a connection error or timeout.
    """
    if isinstance(exc, exceptions.HTTPErrorFFF):
        code = exc.status_code if isinstance(exc.status_code, int) else 0
        return code == 429 or code >= 500
    return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
