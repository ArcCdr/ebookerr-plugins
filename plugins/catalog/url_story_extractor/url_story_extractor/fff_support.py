"""FanFicFare used as a library: one configuration, quiet logs, a clean stdout, plain errors.

Every first-party plugin that imports FanFicFare carries this file byte for byte — a plugin may
import only its own package — and the plugins repository's
``tests/test_fanficfare_support_twins.py`` fails when two copies differ (``LIB-D26``). It gives its
plugin seven things:

* :func:`config_sections` and :func:`build_configuration` — a FanFicFare ``Configuration`` layered
  from FanFicFare's own ``defaults.ini``, the packaged ``base.ini``, the plugin's "Advanced
  FanFicFare options", its settings and per-call overrides; a parse error never quotes a line of
  the options, which may hold a site password;
* :func:`quiet_fanficfare_logging` — importing ``fanficfare`` attaches a DEBUG handler that writes
  to stderr, which the core reports as a WARNING after the plugin's run; once quieted, only
  FanFicFare's WARNING and ERROR records reach the log, re-logged through this plugin's logger;
* :func:`captured_stdout` — the plugin process's stdout carries the core's wire protocol, so what
  FanFicFare prints is captured and logged at DEBUG instead;
* :func:`failure_message`, :func:`masked_text` and :func:`is_outage` — one plain sentence per
  FanFicFare failure, FanFicFare's own text with the username masked, and whether it means the
  site was unreachable;
* :func:`settings_options`, :func:`strip_naming_keys`, :func:`apply_sign_in` and
  :func:`packaged_base_ini` — the plugin's settings, its advanced options without the file-naming
  keys, a stored sign-in and its packaged base options, as FanFicFare options;
* :func:`check_extra_options` — the plugin's save check of its advanced options (``LIB-D47``);
* :func:`can_sign_in` — whether FanFicFare can sign in on a story's site at all (``LIB-D53``).

The route helpers (:func:`challenge_route`, :func:`seed_clearance`, :func:`challenge_outcome`,
:func:`run_routed`) apply a host's browser-check route through FanFicFare's own documented options
(``FR-CHS-10``).
"""

from __future__ import annotations

import configparser
import contextlib
import http.cookiejar
import inspect
import io
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar
from urllib.parse import urlsplit

import fanficfare
import requests
from ebookerr_sdk.challenge import (
    BlockReason,
    ChallengeBlockedError,
    ChallengeRouter,
    Clearance,
    probe,
)
from ebookerr_sdk.spi import SettingsProblem, SiteCredential
from fanficfare import adapters, exceptions
from fanficfare.adapters.base_adapter import BaseSiteAdapter
from fanficfare.configurable import Configuration

logger = logging.getLogger(__name__)

T = TypeVar("T")

FANFICFARE_LOGGER = "fanficfare"
"""The logger every FanFicFare module logs under."""

_PRINTED_MAX_CHARS = 2000
"""At most this many printed characters reach the DEBUG line."""

_NO_OVERRIDES: Mapping[str, str] = MappingProxyType({})
"""The default: no per-call option."""

NAMING_KEYS: frozenset[str] = frozenset(
    {
        "output_filename",
        "output_filename_safepattern",
        "make_directories",
        "zip_output",
        "zip_filename",
        "always_overwrite",
    }
)
"""FanFicFare options that name or place the output file — ebookerr chooses those (``D56``)."""

_DEFAULTS_OPTIONS: tuple[str, ...] = ("is_adult", "include_subject_tags")
"""Plugin settings FanFicFare documents in its ``[defaults]`` section (``C36``)."""

_EPUB_OPTIONS: tuple[str, ...] = ("include_images", "keep_summary_html", "make_firstimage_cover")
"""Plugin settings FanFicFare documents in its ``[epub]`` section (``C36``)."""

CARD_KEYS: frozenset[str] = frozenset(_DEFAULTS_OPTIONS + _EPUB_OPTIONS)
"""The FanFicFare options a plugin's card sets; they win over its advanced options (``LIB-D47``)."""

_OPTION_LINE = re.compile(r"^([^\s:=#;\[][^:=]*?)\s*[:=]")
"""An option line of an ini text: a key starting at column 0, then ``:`` or ``=``."""

_SECTION_LINE = re.compile(r"^\[.+\]")
"""A section line of an ini text, as FanFicFare reads one."""

SIGN_IN_KEYS: frozenset[str] = frozenset({"username", "password", "cookie"})
"""Option keys that hold a site sign-in, which belongs in Settings → Credentials (``LIB-D47``)."""

CREDENTIALS_HREF = "/settings/credentials"
"""The ebookerr page that stores site sign-ins: where a refused sign-in line points."""


class ConfigurationError(RuntimeError):
    """The plugin's FanFicFare options could not be parsed.

    The message names only the exception type.
    """


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


def packaged_base_ini() -> str:
    """Return the plugin's packaged FanFicFare base options (``base.ini``, ``C35``)."""
    return Path(__file__).with_name("base.ini").read_text(encoding="utf-8")


def _ini_value(value: Any) -> str:
    """Render one setting as a FanFicFare option value: booleans or percent-escaped."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).replace("%", "%%")


def settings_options(settings: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Return FanFicFare options from the plugin's settings, by section (``C36``).

    Only the settings present are written: ``is_adult`` and ``include_subject_tags`` under
    ``defaults``; ``include_images``, ``keep_summary_html`` and ``make_firstimage_cover`` under
    ``epub``. :func:`build_configuration` sets them all in ``overrides`` (``LIB-D47``).

    Args:
        settings: The plugin's resolved settings.

    Returns:
        ``{"defaults": {...}, "epub": {...}}``, values rendered as FanFicFare option text.
    """
    options: dict[str, dict[str, str]] = {"defaults": {}, "epub": {}}
    for key in _DEFAULTS_OPTIONS:
        if key in settings:
            options["defaults"][key] = _ini_value(settings[key])
    for key in _EPUB_OPTIONS:
        if key in settings:
            options["epub"][key] = _ini_value(settings[key])
    return options


def strip_naming_keys(text: str) -> str:
    """Return *text* without the option lines that name or place the file (``D56``, ``C37``).

    A dropped key's continuation lines (the indented lines right after it) go with it. Each dropped
    key is logged once per call at WARNING; its value never is.

    Args:
        text: The user's "Advanced FanFicFare options", in FanFicFare's ini format.

    Returns:
        The same text without those lines.
    """
    kept: list[str] = []
    warned: set[str] = set()
    dropping = False
    for line in text.splitlines():
        if dropping and line[:1] in (" ", "\t") and line.strip():
            continue
        dropping = False
        match = _OPTION_LINE.match(line)
        key = match.group(1).strip().lower() if match else ""
        if key in NAMING_KEYS:
            if key not in warned:
                warned.add(key)
                logger.warning(
                    "FanFicFare option %r is ignored: ebookerr chooses file names and folders", key
                )
            dropping = True
            continue
        kept.append(line)
    return "\n".join(kept)


def apply_sign_in(config: Configuration, credential: SiteCredential | None, host: str) -> None:
    """Put a stored sign-in into the configuration's ``overrides`` section (``C36``, ``C37``).

    A ``basic`` sign-in sets FanFicFare's ``username`` and ``password`` (``%`` doubled — its ini
    parser interpolates). Any other kind is not used, with one WARNING; ``None`` sets nothing.

    Args:
        config: The configuration being built.
        credential: The site's stored sign-in (``ctx.credentials``), or ``None``.
        host: The site's host, for the log line.
    """
    if credential is None:
        return
    if credential.kind != "basic":
        logger.warning(
            "FanFicFare uses only a username and password; "
            "the %s sign-in stored for %s is not used",
            credential.kind,
            host,
        )
        return
    if not config.has_section("overrides"):
        config.add_section("overrides")
    config.set("overrides", "username", credential.name.replace("%", "%%"))
    config.set("overrides", "password", credential.value.replace("%", "%%"))
    logger.debug("Using the stored sign-in for %s", host)


_SIGN_IN_BY_CLASS: dict[type, bool] = {}
"""Whether each FanFicFare adapter class can sign in, decided once per class."""

_READS_USERNAME = re.compile(r"""getConfig\(\s*['"]username['"]""")
"""An adapter's source asking FanFicFare's configuration for a ``username``."""


def can_sign_in(url: str) -> bool:
    """Whether FanFicFare's adapter for *url* can sign in (``LIB-D53``).

    True when any class of the adapter's hierarchy other than ``BaseSiteAdapter`` and ``object``
    defines ``performLogin``, or its source reads a username (``getConfig("username")``);
    False for an unknown site or any lookup error. Cached per adapter class.

    Args:
        url: A story or listing address.

    Returns:
        True when a stored username-and-password sign-in would be used on that site.
    """
    try:
        klass = adapters._get_class_for(url)[0]
    except Exception:  # noqa: BLE001 - any lookup error means "no sign-in", never a failure
        return False
    if klass is None:
        return False
    cached = _SIGN_IN_BY_CLASS.get(klass)
    if cached is not None:
        return cached
    # The base adapter and object define nothing site-specific; every other class may log in.
    hierarchy = [c for c in klass.__mro__ if c not in (BaseSiteAdapter, object)]
    found = any("performLogin" in vars(c) for c in hierarchy)
    if not found:
        try:
            found = _READS_USERNAME.search(inspect.getsource(klass)) is not None
        except (OSError, TypeError):
            found = False
    _SIGN_IN_BY_CLASS[klass] = found
    return found


def _listed(words: list[str]) -> str:
    """Join *words* the way a sentence lists them: ``a``, ``a and b``, ``a, b and c``."""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + f" and {words[-1]}"


def _line_numbers(numbers: list[int]) -> str:
    """Name 1-based line numbers: ``line 2``, ``lines 2 and 3``, ``lines 2, 3 and 5``."""
    return ("line " if len(numbers) == 1 else "lines ") + _listed([str(n) for n in numbers])


def _option_lines(text: str) -> list[tuple[int, str]]:
    """Return each option line of *text* as its 1-based number and its lower-case key."""
    return [
        (number, match.group(1).strip().lower())
        for number, line in enumerate(text.splitlines(), start=1)
        if (match := _OPTION_LINE.match(line))
    ]


def _card_warning(owned: list[tuple[int, str]], card_labels: Mapping[str, str]) -> str:
    """Say which lines set what the card sets, by the card's labels, and that the card wins.

    Args:
        owned: The ``(line number, key)`` pairs of the lines a card control overrides.
        card_labels: The plugin's settings labels by field key.

    Returns:
        The sentence.
    """
    keys = list(dict.fromkeys(key for _number, key in owned))
    labels = _listed([f'"{card_labels[key]}"' for key in keys])
    verb = "is" if len(keys) == 1 else "are"
    lines = _line_numbers([number for number, _key in owned])
    return f"{lines}: {_listed(keys)} {verb} set by {labels} on this card, which wins"


def _parse_problem(text: str, exc: configparser.ParsingError) -> str:
    """Say what is wrong with the first line FanFicFare could not read — its number, never its text.

    Args:
        text: The options that were read.
        exc: FanFicFare's parse error; its ``errors`` hold ``(line number, line)`` pairs.

    Returns:
        The sentence.
    """
    number = exc.errors[0][0]
    if not any(_SECTION_LINE.match(line) for line in text.splitlines()[: number - 1]):
        return f"line {number}: an option needs a section line above it, such as [defaults]"
    return f"line {number}: FanFicFare cannot read this line — write an option as name: value"


def check_extra_options(
    field: str, text: str, card_labels: Mapping[str, str]
) -> list[SettingsProblem]:
    """Check the user's "Advanced FanFicFare options" before they are saved (``LIB-D47``).

    FanFicFare's own parser reads the text, as a pull does: the first line it cannot read refuses
    the save, by its number. An option line holding a sign-in — ``username``, ``password`` or
    ``cookie``, in any section — refuses it too, with a link to Settings → Credentials. An option
    line setting what one of the card's own controls sets (:data:`CARD_KEYS`) saves with a
    warning naming that control, whose value wins. No message quotes a line of the text, which may
    hold a password.

    Args:
        field: The key of the settings field that holds the text.
        text: The text the save would store.
        card_labels: The plugin's settings labels by field key; a :data:`CARD_KEYS` key among them
            is one of its card controls.

    Returns:
        One blocking problem for an unreadable line and one for the sign-in lines, then one warning
        for the lines the card overrides, in that order; empty when the text is fine.
    """
    quiet_fanficfare_logging()
    problems: list[SettingsProblem] = []
    try:
        Configuration(["unknown"], "epub", lightweight=True).read_string(text)
    except configparser.ParsingError as exc:
        problems.append(SettingsProblem(field, _parse_problem(text, exc)))
    except Exception as exc:  # noqa: BLE001 — none may leak its text
        message = f"FanFicFare cannot read these options ({type(exc).__name__})"
        problems.append(SettingsProblem(field, message))
    lines = _option_lines(text)
    sign_ins = [number for number, key in lines if key in SIGN_IN_KEYS]
    if sign_ins:
        message = (
            f"{_line_numbers(sign_ins)}: sign-ins belong in Settings → Credentials, "
            "not in these options"
        )
        problems.append(SettingsProblem(field, message, href=CREDENTIALS_HREF))
    owned = [(number, key) for number, key in lines if key in CARD_KEYS and key in card_labels]
    if owned:
        problems.append(SettingsProblem(field, _card_warning(owned, card_labels), blocking=False))
    return problems


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


SOLVER_TIMEOUT_OPTION = "60000"
"""FanFicFare's flaresolverr_proxy_timeout in milliseconds: the SDK's solve budget."""


def solver_options(solver_url: str) -> dict[str, str]:
    """FanFicFare's options that send a site's pages through the solver at *solver_url* (FR-CHS-10).

    use_flaresolverr_proxy is "directimages" (images go direct); flaresolverr_proxy_protocol is the
    address's scheme; flaresolverr_proxy_address its host (an IPv6 host in brackets);
    flaresolverr_proxy_port its port, 443 for https and 80 for http when it names none;
    flaresolverr_proxy_timeout is SOLVER_TIMEOUT_OPTION. A path in the address is not used:
    FanFicFare asks <protocol>://<address>:<port>/v1.
    """
    parts = urlsplit(solver_url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return {
        "use_flaresolverr_proxy": "directimages",
        "flaresolverr_proxy_protocol": parts.scheme,
        "flaresolverr_proxy_address": host,
        "flaresolverr_proxy_port": str(port),
        "flaresolverr_proxy_timeout": SOLVER_TIMEOUT_OPTION,
    }


def challenge_route(
    router: ChallengeRouter | None, host: str
) -> tuple[dict[str, str], Clearance | None]:
    """The FanFicFare options and the clearance a configuration for *host* takes from its route.

    No router: ({}, None). A host marked every page: router.require_solver(host), then
    (solver_options(router.solver_url), None). A kept clearance: ({"user_agent": <its User-Agent,
    rendered by _ini_value>}, clearance). Otherwise ({}, None).

    Raises:
        ChallengeBlockedError: The host is marked every page and the solver is unset or does not
            answer.
    """
    if router is None:
        return {}, None
    if router.every_page(host):
        router.require_solver(host)
        # require_solver passed, so the address is set
        return solver_options(router.solver_url or ""), None
    clearance = router.clearance(host)
    if clearance is not None:
        return {"user_agent": _ini_value(clearance.user_agent)}, clearance
    return {}, None


def seed_clearance(config: Configuration, clearance: Clearance, url: str) -> None:
    """Put the clearance's cookies for *url*'s host into the configuration's own cookie jar.

    Call it on a complete configuration: Configuration.get_cookiejar() creates FanFicFare's
    fetcher from the options set so far. A cookie is secure when *url* is https.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    secure = parts.scheme == "https"
    jar = config.get_cookiejar()
    for c in clearance.cookies:
        if not c.matches(host):
            continue
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=c.name,
                value=c.value,
                port=None,
                port_specified=False,
                domain=c.domain,
                domain_specified=True,
                domain_initial_dot=c.domain.startswith("."),
                path=c.path,
                path_specified=True,
                secure=secure,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )


def challenge_outcome(
    exc: BaseException, url: str, router: ChallengeRouter | None, *, retried: bool = False
) -> bool | str:
    """What a FanFicFare failure on *url* means for its route (FR-CHS-10).

    Only an HTTPErrorFFF is looked at. On a host marked every page a 403 or a 428 (FanFicFare's
    code for a solver that answered without passing) is the could-not-pass sentence. On the first
    run a 403 on any other host is probed, a ChallengeBlockedError from the probe becoming its
    sentence. On the rerun (*retried*) a 403 on a host holding a kept clearance means the site
    refused that clearance to FanFicFare's own client: the host is marked every page, so the next
    run goes through the solver. Anything else is False.

    Args:
        exc: What FanFicFare raised.
        url: The address FanFicFare was given.
        router: The plugin call's router; None when the plugin passes no browser check.
        retried: Whether *exc* comes from the run after a routed one.

    Returns:
        True to run FanFicFare once more, a sentence (FR-CHS-5) to fail with, or False to keep
        the failure as it is.
    """
    if router is None or not isinstance(exc, exceptions.HTTPErrorFFF):
        return False
    code = exc.status_code if isinstance(exc.status_code, int) else 0
    host = (urlsplit(url).hostname or "").lower()
    if router.every_page(host):
        if code in (403, 428):
            return str(ChallengeBlockedError(host, BlockReason.NOT_PASSED))
        return False
    if code != 403:
        return False
    if retried:
        if router.clearance(host) is None:
            return False
        router.mark_every_page(host)
        return True
    try:
        return probe(url, router)
    except ChallengeBlockedError as blocked:
        return str(blocked)


class ChallengeRefusedError(RuntimeError):
    """A site's browser check ended FanFicFare's runs; its message is the sentence (FR-CHS-5)."""


MAX_RERUNS = 2
"""Reruns after FanFicFare's first run: once the probe routed the host, once through the solver."""


def run_routed(
    run: Callable[[], T], url: str, router: ChallengeRouter | None, *, on_rerun: Callable[[], None]
) -> T:
    """Run FanFicFare through *run*, and again while its site's browser check gets routed.

    A failure :func:`challenge_outcome` answers True for runs *run* again after *on_rerun* (which
    drops what the plugin cached for the host and logs the rerun), at most MAX_RERUNS times: once
    the probe routed the host, and once through the solver when the site refused the clearance to
    FanFicFare's own client (FR-CHS-10).

    Args:
        run: One whole FanFicFare run (it builds its own configuration).
        url: The address FanFicFare is given.
        router: The plugin call's router; None runs *run* once.
        on_rerun: Called before each rerun.

    Returns:
        What *run* returns.

    Raises:
        ChallengeRefusedError: The browser check could not be passed; carries the sentence.
        Exception: The last run's own failure, when it is not a browser check.
    """
    reruns = 0
    while True:
        try:
            return run()
        except ChallengeBlockedError as blocked:
            raise ChallengeRefusedError(str(blocked)) from blocked
        except Exception as exc:
            outcome = challenge_outcome(exc, url, router, retried=reruns > 0)
            if isinstance(outcome, str):
                raise ChallengeRefusedError(outcome) from exc
            if outcome is False or reruns == MAX_RERUNS:
                raise
        reruns += 1
        on_rerun()


def build_configuration(
    sections: list[str],
    *,
    fileform: str,
    base_ini: str,
    options: Mapping[str, Mapping[str, str]],
    extra_options: str,
    overrides: Mapping[str, str] = _NO_OVERRIDES,
    lightweight: bool = False,
) -> Configuration:
    """Build a FanFicFare configuration, each layer overriding the ones before it (``C36``).

    The order: FanFicFare's own ``defaults.ini``, then *base_ini*, then *extra_options* without the
    file-naming keys (:func:`strip_naming_keys`), then *options*, then *overrides*. *options* and
    *overrides* are set in FanFicFare's ``overrides`` section, which wins over every site and format
    section, so the card's settings decide whatever the advanced options say (``LIB-D47``). A
    stored sign-in goes on top of the result with :func:`apply_sign_in`.

    Args:
        sections: What :func:`config_sections` returned for the address.
        fileform: The output format FanFicFare's per-format sections are keyed by.
        base_ini: The plugin's base options in FanFicFare's ini format (:func:`packaged_base_ini`).
        options: The plugin's settings as FanFicFare options, by the section FanFicFare documents
            them in (:func:`settings_options`); each is set in ``overrides``.
        extra_options: The user's "Advanced FanFicFare options", in FanFicFare's ini format.
        overrides: Option name to value, set in FanFicFare's ``overrides`` section, which wins over
            every other layer.
        lightweight: FanFicFare's lightweight mode (no output-format machinery).

    Returns:
        The configuration.

    Raises:
        ConfigurationError: *base_ini* or *extra_options* could not be parsed. The message carries
            only the exception's type name and the original is not chained — a parse error quotes
            the offending line, which may be a site login.
    """
    config = Configuration(sections, fileform, lightweight=lightweight)
    defaults_ini = Path(fanficfare.__file__).parent / "defaults.ini"
    try:
        config.read([str(defaults_ini)])
        config.read_string(base_ini)
    except Exception as exc:  # noqa: BLE001 — none may leak its text
        raise ConfigurationError(
            f"Could not parse FanFicFare configuration: {type(exc).__name__}"
        ) from None
    # the user's advanced options, naming keys dropped, win over the base options
    try:
        config.read_string(strip_naming_keys(extra_options))
    except Exception as exc:  # noqa: BLE001 — none may leak its text
        raise ConfigurationError(
            f"Could not read the Advanced FanFicFare options: {type(exc).__name__}"
        ) from None
    if not config.has_section("overrides"):
        config.add_section("overrides")
    # the card's settings win over every section of the advanced options; per-call ones over them
    for values in options.values():
        for key, value in values.items():
            config.set("overrides", key, value)
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


def _site(url: str) -> str:
    """Name *url*'s site by its host without a leading ``www.``: ``example.org``."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host or "the site"


def failure_message(exc: BaseException, url: str) -> str:  # noqa: C901
    """Return one plain, user-facing sentence for a FanFicFare failure on *url* (``LIB-D25``).

    A refused login names the site and says whether a sign-in was stored — FanFicFare names the
    username it tried, empty when it had none (``LIB-D49``); a failed download is one fixed
    sentence naming the site.

    Args:
        exc: What FanFicFare, or its HTTP client, raised.
        url: The address it was working on.

    Returns:
        The sentence. It never carries FanFicFare's own text, which may name the username: an
        exception this table does not know names only its type.
    """
    if isinstance(exc, exceptions.UnknownSite):
        return f"unsupported site: FanFicFare does not support {url}"
    if isinstance(exc, exceptions.StoryDoesNotExist):
        return f"story not found: {url}"
    if isinstance(exc, exceptions.AccessDenied):
        return "the site refused access to this story"
    if isinstance(exc, exceptions.FailedToLogin):
        if exc.username:
            return f"{_site(url)} refused the stored sign-in — check it in Settings → Credentials"
        return (
            f"{_site(url)} needs a sign-in and none is stored — add one in Settings → Credentials"
        )
    if isinstance(exc, exceptions.AdultCheckRequired):
        return (
            'the site asks you to confirm you are an adult — turn on "Confirm adult content" in '
            "this plugin's settings"
        )
    if isinstance(exc, exceptions.NeedTimedOneTimePassword):
        return "the site asks for a one-time password, which a background download cannot give"
    if isinstance(exc, exceptions.InvalidStoryURL):
        return f"not a story address FanFicFare recognises: {url}"
    if isinstance(exc, exceptions.HTTPErrorFFF):
        return f"the site answered HTTP {exc.status_code}"
    if isinstance(exc, exceptions.FailedToDownload):
        return f"the download from {_site(url)} failed"
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "the site could not be reached"
    if isinstance(exc, ConfigurationError):
        return str(exc)
    return f"FanFicFare failed ({type(exc).__name__})"


def masked_text(exc: BaseException) -> str:
    """Return FanFicFare's own text for *exc*, the username it names replaced by ``***``.

    ``FailedToLogin`` names the username it tried; this is the only form of its text that may
    reach a log line (``LIB-D49``).

    Args:
        exc: What FanFicFare, or its HTTP client, raised.

    Returns:
        The exception's text.
    """
    text = str(exc)
    username = getattr(exc, "username", None)
    return text.replace(username, "***") if isinstance(username, str) and username else text


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
