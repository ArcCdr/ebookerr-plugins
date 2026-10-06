"""My Literotica catalog: the activity-wall API behind the plugin.

``plugin.MyLiteroticaPlugin`` serves the SPI ``scan`` call through ``ebookerr_sdk.host``: it asks
the core for the stored literotica.com sign-in with ``ctx.credentials`` and turns the story dicts
``scan`` returns here into ``StoryPatch`` objects. This module holds everything that talks to
Literotica and shapes its answers, and nothing of the plugin wire: it is handed the sign-in and
the plugin context, whose ``report`` and ``circuit`` the SDK host turns into frames.

``login`` logs in to the auth host and returns the durable ``sessionid`` cookie value.
``mint_token`` exchanges that cookie for a fresh one-hour bearer token. ``resolve_token``
turns the stored sign-in into a wall bearer token. ``fetch_wall_page`` reads one page of the
activity wall. ``fetch_activities`` drains the activity wall page by page until exhausted.
``map_activity`` maps one wall entry to a story dict (the ``StoryPatch`` fields; dates as
``YYYY-MM-DD`` strings). ``scan`` enumerates every story publication the wall holds, reporting
progress twice.

The breaker is the core's, reached through the context's ``CircuitGuard`` (EXP-269) and keyed by
the host (``CIRCUIT_KEY``). When it is open, ``fetch_wall_page`` and ``mint_token`` raise
``CatalogHostUnreachable`` before any request is made and ``scan`` lets it escape, so the plugin
answers an empty scan with a warning. The sibling literotica_stories catalog plugin shares the
same circuit breaker, so one host outage is one breaker, not two.
"""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from ebookerr_sdk.spi import CircuitGuard, CircuitOpenError, PluginContext, SiteCredential


class CatalogHostUnreachable(Exception):  # noqa: N818
    """The host's circuit breaker is open, so this scan made no request at all."""


# Shared verbatim with the sibling `literotica_stories` catalog plugin; plugins run as separate
# processes and cannot import a shared module. The key is the **host**, so both plugins share
# one breaker and one outage (EXP-269, R3).
CIRCUIT_KEY = "host:literotica.com"
CIRCUIT_LABEL = "literotica.com"

API_BASE = "https://literotica.com/api/3"
WALL_URL = API_BASE + "/activity/wall"
AUTH_LOGIN_URL = (
    "https://auth.literotica.com/login?redirect=www.literotica.com"
    "&err_redirect=https%3A%2F%2Fwww.literotica.com%2Fauthenticate%2Flogin"
)
AUTH_CHECK_URL = "https://auth.literotica.com/check"
SITE_BASE = "https://www.literotica.com"
SIGN_IN_URL = "https://www.literotica.com/"
"""The URL the plugin asks the core for the stored sign-in of (``ctx.credentials``)."""
PAGE_SIZE = 50
MAX_PAGES = 10
STORY_ACTION = "published-story"
_UA = "Mozilla/5.0 (ebookerr MyLiterotica catalog plugin)"
_TIMEOUT_S = 30
_TYPE_PREFIX = {"story": "s", "audio": "s", "poem": "p", "illustration": "i"}
_LANGUAGE_NAMES = {1: "en"}
# Literotica's own category display names, keyed by the integer id the activity wall returns in
# each `what.category` field. The API exposes no display name anywhere - `category_info` carries
# only a URL slug, and title-casing that slug produced internal-looking values
# ("Non Consent Stories") that disagreed with both the site and FanFicFare
# ("Reluctance/NonConsent"). Shared verbatim with the sibling `literotica_stories` catalog
# plugin; plugins run as separate processes and cannot import a shared module.
_CATEGORY_NAMES = {
    2: "Erotic Couplings",
    3: "Reviews & Essays",
    4: "Exhibitionist & Voyeur",
    5: "Fetish",
    6: "Gay Male",
    7: "Group Sex",
    8: "How To",
    9: "Taboo/Incest",
    10: "Interracial Love",
    11: "Lesbian Sex",
    12: "Loving Wives",
    13: "Reluctance/NonConsent",
    14: "NonHuman",
    15: "Romance",
    16: "Toys & Masturbation",
    17: "Erotic Poetry",
    26: "Mature",
    27: "Fan Fiction & Celebrities",
    28: "Chain Stories",
    29: "Mind Control",
    31: "BDSM",
    32: "Non-English",
    33: "Novels and Novellas",
    34: "Humor & Satire",
    35: "Non-Erotic",
    36: "Non-Erotic Poetry",
    37: "Anal",
    38: "Sci-Fi & Fantasy",
    39: "Audio",
    40: "First Time",
    45: "Illustrated",
    46: "Poetry With Audio",
    47: "Illustrated Poetry",
    48: "Transgender",
    51: "Erotic Horror",
    53: "Letters & Transcripts",
    55: "Erotic Art",
    56: "Adult Comics",
    57: "Public Domain",
    58: "Crossdressing",
}


def story_url(story_type: object, slug: str) -> str:
    """Build the canonical Literotica URL for a content slug.

    Literotica serves each content kind under its own prefix - stories and audio under ``/s/``,
    poems under ``/p/``, illustrations under ``/i/``. An unknown or missing type falls back to
    ``/s/``, which Literotica redirects to the right prefix.

    Args:
        story_type: The ``what.type`` value from the activity payload.
        slug: The ``what.url`` slug, e.g. ``"sample-omicron-the-aftermath"``.

    Returns:
        The absolute story URL.
    """
    prefix = _TYPE_PREFIX.get(str(story_type), "s")
    return f"{SITE_BASE}/{prefix}/{slug}"


def build_custom_fields(what: dict[str, Any], when: object) -> dict[str, Any]:  # noqa: C901
    """Extract the Stories-page custom columns from one wall payload.

    Labels are shared verbatim with the sibling ``literotica_stories`` catalog plugin wherever
    the two report the same value, so both catalogs contribute the same columns.

    Args:
        what: The activity's ``what`` object.
        when: The activity's ``when`` unix timestamp.

    Returns:
        A label-keyed mapping of ``str``/``int``/``float``/``bool`` values.
    """
    custom: dict[str, Any] = {}

    # Engagement counters - identical labels to the literotica_stories catalog plugin.
    for label, key in (
        ("Votes", "rate_count"),
        ("Views", "view_count"),
        ("Favorites", "favorite_count"),
        ("Comments", "comment_count"),
        ("Rank", "rank"),
        ("Reading Lists", "reading_lists_count"),
    ):
        value = what.get(key)
        if value is not None:
            custom[label] = value

    # Flags - the API mixes 0/1 ints and real booleans, so coerce every one.
    for label, key in (
        ("Is Hot", "is_hot"),
        ("Is New", "is_new"),
        ("Writer's Pick", "writers_pick"),
        ("Contest Winner", "contest_winner"),
        ("Downloadable", "allow_download"),
        ("Voting Enabled", "allow_vote"),
        ("Comments Enabled", "enable_comments"),
    ):
        if key in what:
            custom[label] = bool(what[key])

    if what.get("language") is not None:
        custom["Language"] = _LANGUAGE_NAMES.get(what["language"], str(what["language"]))

    if what.get("type"):
        custom["Type"] = str(what["type"])

    # The wall gives the whole series, so the part count is exact rather than reported.
    series = what.get("series")
    if isinstance(series, dict) and isinstance(series.get("items"), list):
        custom["Series Parts"] = len(series["items"])

    author = what.get("author")
    if isinstance(author, dict):
        for label, key in (
            ("Author Stories", "stories_count"),
            ("Author Poems", "poems_count"),
            ("Author Audios", "audios_count"),
            ("Author Illustrations", "illustrations_count"),
        ):
            value = author.get(key)
            if value is not None:
                custom[label] = value

    # The wall's own ordering key, shown in the app's sole display format.
    if isinstance(when, int):
        custom["Announced"] = datetime.fromtimestamp(when, UTC).isoformat(timespec="seconds")

    return custom


def _add_optional_fields(patch: dict[str, Any], what: dict[str, Any]) -> None:
    """Add title, description, rating and word count to the patch when present."""
    if what.get("title"):
        patch["title"] = what["title"]
    if what.get("description"):
        patch["description"] = what["description"]
    if what.get("rate_all") is not None:
        patch["rating"] = what["rate_all"]
    if what.get("words_count") is not None:
        patch["num_words"] = what["words_count"]


def _add_author_fields(patch: dict[str, Any], what: dict[str, Any]) -> None:
    """Add the author name and profile URL, falling back to the flat ``authorname``."""
    author = what.get("author")
    username = (author.get("username") if isinstance(author, dict) else None) or what.get(
        "authorname"
    )
    if username:
        patch["author"] = username
        patch["author_url"] = f"{SITE_BASE}/authors/{username}"


def category_name(what: dict[str, Any]) -> str | None:
    """Return Literotica's own display name for an activity's category, or None when unmapped.

    Args:
        what: The activity's ``what`` object.

    Returns:
        The mapped display name, or ``None`` when ``category`` is missing, non-numeric, or an
        id Literotica added after this table was written.
    """
    try:
        return _CATEGORY_NAMES[int(what["category"])]
    except (KeyError, TypeError, ValueError):
        return None


def category_label(what: dict[str, Any]) -> str | None:
    """Display label for the category: Literotica's own name, else the slug title-cased.

    The fallback keeps an unmapped category readable rather than dropping it, which matters
    because Literotica adds categories without notice.

    Args:
        what: The activity's ``what`` object.

    Returns:
        The category label, or ``None`` when the payload carries neither a mapped id nor a URL
        slug.
    """
    mapped = category_name(what)
    if mapped is not None:
        return mapped
    slug = (what.get("category_info") or {}).get("pageUrl")
    if not slug:
        return None
    return " ".join(word.capitalize() for word in str(slug).split("-"))


def _add_category_and_tags(patch: dict[str, Any], what: dict[str, Any]) -> None:
    """Add Literotica's own category display name and the comma-joined tag list."""
    category = category_label(what)
    if category:
        patch["category"] = category

    tags = [str(t["tag"]) for t in what.get("tags", []) if isinstance(t, dict) and t.get("tag")]
    if tags:
        patch["tags"] = ", ".join(tags)


def _add_date_published(patch: dict[str, Any], what: dict[str, Any]) -> None:
    """Add ``date_published`` in ISO form, parsed from the MM/DD/YYYY ``date_approve`` field.

    Emits ISO so both Literotica catalogs agree byte for byte; a missing or unparseable
    ``date_approve`` leaves the field unset rather than raising.
    """
    try:
        approved = datetime.strptime(what["date_approve"], "%m/%d/%Y")  # noqa: DTZ007
        patch["date_published"] = approved.date().isoformat()
    except (KeyError, TypeError, ValueError):
        pass


def _add_series_fields(patch: dict[str, Any], what: dict[str, Any]) -> None:
    """Add the series title and URL when the activity belongs to a series."""
    series = what.get("series")
    if isinstance(series, dict):
        meta = series.get("meta") or {}
        if meta.get("title"):
            patch["series"] = meta["title"]
        if meta.get("id"):
            patch["series_url"] = f"{SITE_BASE}/series/se/{meta['id']}"


def map_activity(activity: dict[str, Any]) -> dict[str, Any] | None:
    """Map one activity-wall entry to a story dict (the ``StoryPatch`` fields).

    Only ``published-story`` activities produce a story; every other activity kind, and any
    malformed payload, returns ``None``.
    Every story declares ``metadata_fetched=True``: a listing row already carries the full
    metadata the site offers, so the core never offers it to a metadata pass (SPI 2.11).

    Args:
        activity: One entry from the wall response's ``data`` list.

    Returns:
        The story dict, or ``None`` when the activity is not a usable publication.
    """
    if activity.get("action") != STORY_ACTION:
        return None

    what = activity.get("what")
    if not isinstance(what, dict):
        return None

    slug = what.get("url")
    story_id = what.get("id")
    if not slug or story_id is None:
        return None

    patch: dict[str, Any] = {
        "url": story_url(what.get("type"), slug),
        "story_id": str(story_id),
        "site": "literotica.com",
        "metadata_fetched": True,
    }

    _add_optional_fields(patch, what)
    _add_author_fields(patch, what)
    _add_category_and_tags(patch, what)
    _add_date_published(patch, what)
    _add_series_fields(patch, what)

    custom = build_custom_fields(what, activity.get("when"))
    if custom:
        patch["custom"] = custom

    return patch


class _CallFailedError(Exception):
    """Internal marker reporting a failed call to the host's breaker."""


def _record(circuit: CircuitGuard, *, ok: bool) -> bool:
    """Report one call's outcome against this host's breaker; return whether it is open now.

    The SDK host's guard asks, records and answers over the same ``circuit`` frames the script
    wrote by hand (``EXP-269``): an answered call is a success, a transport failure a failure.
    """
    try:
        with circuit.guard(CIRCUIT_KEY, label=CIRCUIT_LABEL):
            if not ok:
                raise _CallFailedError
    except (CircuitOpenError, _CallFailedError):
        pass
    return circuit.is_open(CIRCUIT_KEY)


def fetch_wall_page(
    token: str, last_id: str | None, *, circuit: CircuitGuard
) -> list[dict[str, Any]]:
    """Fetch one page of the activity wall, guarded by the host's circuit breaker.

    Asks the core once before spending a request: a host the app has already found unreachable
    is not asked again (EXP-269). The call's outcome is reported back, so the breaker opens on a
    run of transport failures and closes on the first success. An HTTP status is **never** a
    breaker failure - a 401 is a rejected token and neither it nor any other answer means the
    host stopped answering.

    Args:
        token: The bearer token.
        last_id: The previous page's last activity ``id``, or ``None`` for the first page.
        circuit: The plugin context's circuit guard, which holds this host's breaker.

    Returns:
        The page's ``data`` list (empty when the wall is exhausted).

    Raises:
        CatalogHostUnreachable: When the host's circuit breaker is open.
        urllib.error.HTTPError: If the wall rejects the token or returns an error status.
        urllib.error.URLError: If the wall could not be reached (transport error).
        TimeoutError: If the request times out.
        RuntimeError: If the response is unparseable JSON.
    """
    if circuit.is_open(CIRCUIT_KEY):
        raise CatalogHostUnreachable(CIRCUIT_LABEL)

    params: dict[str, Any] = {"chunked": 1, "limit": PAGE_SIZE}
    if last_id:
        params["last_id"] = last_id
    query = urllib.parse.quote(json.dumps(params, separators=(",", ":")))
    request = urllib.request.Request(  # noqa: S310
        f"{WALL_URL}?params={query}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        # The server answered. Absence and misconfiguration are not an outage, so the
        # breaker is told the call succeeded.
        _record(circuit, ok=True)
        raise
    except (urllib.error.URLError, TimeoutError):
        open_now = _record(circuit, ok=False)
        if open_now:
            raise CatalogHostUnreachable(CIRCUIT_LABEL) from None
        raise
    except json.JSONDecodeError as exc:
        msg = "Literotica activity wall returned malformed JSON"
        raise RuntimeError(msg) from exc

    _record(circuit, ok=True)
    data = payload.get("data")
    return data if isinstance(data, list) else []


def fetch_activities(
    token: str, *, circuit: CircuitGuard
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drain the activity wall page by page until it is exhausted.

    The endpoint holds only the 200 most recent activities, so this always terminates well
    before ``MAX_PAGES``; the cap is a safety net against an endpoint that never shortens.
    Deduplicated by ``id``, both within a page and across pages: the wall's own raw
    responses have been observed repeating an id back-to-back inside a single page, on top
    of a boundary-inclusive ``last_id`` cursor that reopens a page's last few activities as
    the start of the next one — a blind concatenation would carry both kinds of duplicate
    into the result.

    Args:
        token: The bearer token.
        circuit: The plugin context's circuit guard, which guards every page request.

    Returns:
        A tuple of (activities, log entries).
    """
    logs: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    last_id: str | None = None

    for page in range(1, MAX_PAGES + 1):
        items = fetch_wall_page(token, last_id, circuit=circuit)
        new_items = []
        for item in items:
            item_id = item.get("id")
            if item_id and item_id in seen_ids:
                continue
            new_items.append(item)
            if item_id:
                seen_ids.add(item_id)
        activities.extend(new_items)
        logs.append(
            {
                "level": "debug",
                "message": f"Activity wall page {page}: {len(items)} activities",
            }
        )
        if len(items) < PAGE_SIZE:
            break
        last_id = items[-1].get("id")
        if not last_id:
            logs.append(
                {
                    "level": "warning",
                    "message": (
                        f"Activity wall page {page} carried no cursor id;"
                        f" stopping after {len(activities)} activities"
                    ),
                }
            )
            break
    else:
        logs.append(
            {
                "level": "warning",
                "message": (
                    f"Activity wall page cap reached ({MAX_PAGES} pages,"
                    f" {len(activities)} activities); older activities were not read"
                ),
            }
        )

    return activities, logs


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses every redirect, turning a 3xx into an ``HTTPError``."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Return None so urllib raises instead of following the redirect."""
        return None


def login(username: str, password: str) -> str:
    """Log in to auth.literotica.com and return the durable ``sessionid`` cookie value.

    A successful login answers ``200`` with the body ``OK`` and sets a one-year ``sessionid``
    cookie; wrong credentials answer ``303`` with no cookie, so redirects are never followed.

    Args:
        username: The Literotica account name.
        password: The Literotica account password.

    Returns:
        The ``sessionid`` cookie value.

    Raises:
        RuntimeError: If the credentials are rejected, the response is unexpected, or no
            ``sessionid`` cookie is issued.
    """
    body = urllib.parse.urlencode(
        {
            "login": username,
            "password": password,
            "return_to": "www.literotica.com",
            "form_url": "https://www.literotica.com/authenticate/login",
        }
    ).encode()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
    request = urllib.request.Request(  # noqa: S310
        AUTH_LOGIN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _UA},
    )
    try:
        with opener.open(request, timeout=_TIMEOUT_S) as response:
            status = response.status
            text = response.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            msg = "Literotica rejected the stored username or password"
            raise RuntimeError(msg) from exc
        msg = f"Literotica login failed: HTTP {exc.code}"
        raise RuntimeError(msg) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        msg = f"Literotica login could not be reached: {exc}"
        raise RuntimeError(msg) from exc

    if status != 200 or text != "OK":
        msg = "Literotica rejected the stored username or password"
        raise RuntimeError(msg)

    for cookie in jar:
        if cookie.name == "sessionid":
            return str(cookie.value)

    msg = "Literotica login succeeded but issued no session cookie"
    raise RuntimeError(msg)


def mint_token(sessionid: str, *, circuit: CircuitGuard) -> str:
    """Exchange a Literotica ``sessionid`` cookie for a fresh one-hour bearer token.

    The auth service answers with the raw JWT as the response body (not JSON). The request is
    guarded by the host's circuit breaker exactly as :func:`fetch_wall_page`'s is.

    Args:
        sessionid: The ``sessionid`` cookie value obtained from :func:`login`.
        circuit: The plugin context's circuit guard, which holds this host's breaker.

    Returns:
        The bearer token, without the ``Bearer `` prefix.

    Raises:
        CatalogHostUnreachable: When the host's circuit breaker is open.
        urllib.error.HTTPError: If the session is rejected.
        urllib.error.URLError: If the auth service could not be reached (transport error).
        TimeoutError: If the request times out.
        RuntimeError: If the response body is not a valid JWT.
    """
    if circuit.is_open(CIRCUIT_KEY):
        raise CatalogHostUnreachable(CIRCUIT_LABEL)

    url = f"{AUTH_CHECK_URL}?timestamp={int(time.time())}"
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "Cookie": f"sessionid={sessionid}",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            token = response.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError:
        # The server answered. Absence and misconfiguration are not an outage, so the
        # breaker is told the call succeeded.
        _record(circuit, ok=True)
        raise
    except (urllib.error.URLError, TimeoutError):
        open_now = _record(circuit, ok=False)
        if open_now:
            raise CatalogHostUnreachable(CIRCUIT_LABEL) from None
        raise

    _record(circuit, ok=True)
    if token.count(".") != 2:
        msg = "Literotica token refresh returned an unexpected response"
        raise RuntimeError(msg)
    return str(token)


def resolve_token(credential: SiteCredential | None, *, circuit: CircuitGuard) -> tuple[str, bool]:
    """Turn the stored literotica.com sign-in into a wall bearer token.

    Two sign-in shapes are supported: ``basic`` (username + password, which logs in and mints a
    fresh one-hour token on every scan) and a ``cookie`` named ``auth_token`` (a token pasted
    from the browser, which expires within the hour).

    Args:
        credential: The stored literotica.com sign-in, or ``None`` when the core holds none.
        circuit: The plugin context's circuit guard, which guards the minting request.

    Returns:
        A tuple of (bearer token, ``True`` when the token was freshly minted from credentials).

    Raises:
        RuntimeError: If no sign-in is stored or its shape is unsupported.
    """
    if credential is None or not credential.value:
        msg = (
            "Literotica credentials not configured - add a literotica.com profile"
            " under Settings → Credentials"
        )
        raise RuntimeError(msg)
    if credential.kind == "basic":
        return mint_token(login(credential.name, credential.value), circuit=circuit), True
    if credential.kind == "cookie" and credential.name == "auth_token":
        return credential.value, False
    msg = (
        "Literotica profile must be kind 'basic' (username + password) or a cookie named"
        " 'auth_token' - see Settings → Credentials"
    )
    raise RuntimeError(msg)


def _track_unmapped_category(
    what: dict[str, Any], unmapped_categories: set[tuple[str, str]]
) -> None:
    """Record an activity's category id if it has no entry in ``_CATEGORY_NAMES``.

    Args:
        what: One activity's ``what`` mapping.
        unmapped_categories: Set of (category id, category slug) pairs, updated in place.
    """
    if category_name(what) is None:
        slug = (what.get("category_info") or {}).get("pageUrl")
        if slug:
            unmapped_categories.add((str(what.get("category")), str(slug)))


def _unmapped_categories_log(unmapped_categories: set[tuple[str, str]]) -> dict[str, Any] | None:
    """Build one aggregated warning log entry for a scan's unmapped category ids.

    Args:
        unmapped_categories: Set of (category id, category slug) pairs seen during the scan.

    Returns:
        A log entry dict, or None when every category id seen during the scan was mapped.
    """
    if not unmapped_categories:
        return None
    listed = ", ".join(f"{cat_id}={slug}" for cat_id, slug in sorted(unmapped_categories))
    return {
        "level": "warning",
        "message": (
            f"{len(unmapped_categories)} Literotica category id(s) are not in this"
            f" plugin's name table, so their slug was title-cased instead: {listed}"
        ),
    }


def scan(
    credential: SiteCredential | None, ctx: PluginContext
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scan the Literotica activity wall and return every story publication it holds.

    The endpoint exposes only the 200 most recent activities, so the whole wall is drained on
    every scan; there is no look-back setting to honour.

    Args:
        credential: The stored literotica.com sign-in, or ``None`` when the core holds none.
        ctx: The plugin context: its ``circuit`` guards every request and its ``report`` carries
            the progress, 50 % once the wall is read and 100 % when the scan ends.

    Returns:
        A tuple of (story dicts, log entries) where log entries are
        ``{"level": str, "message": str}`` dicts.

    Raises:
        CatalogHostUnreachable: The host's circuit breaker is open, so no request was made.
        RuntimeError: No usable credential is stored, the wall rejected the token, or the wall
            could not be fetched. Raised — never reported as an empty wall — so the core fails
            the scan and keeps the stored catalog (standing rule, EXP-073).
    """
    logs: list[dict[str, Any]] = []

    token, minted = resolve_token(credential, circuit=ctx.circuit)

    logs.append(
        {
            "level": "info",
            "message": (
                "Authenticated with literotica.com using the stored username and password"
                if minted
                else "Using the stored literotica.com auth_token (valid for about one hour)"
            ),
        }
    )

    activities, fetch_logs = fetch_activities(token, circuit=ctx.circuit)
    logs.extend(fetch_logs)

    ctx.report(50.0)

    stories: list[dict[str, Any]] = []
    seen: set[str] = set()
    publications = 0
    duplicates = 0
    unmapped_categories: set[tuple[str, str]] = set()

    for activity in activities:
        story = map_activity(activity)
        if story is None:
            continue
        publications += 1
        what = activity.get("what") or {}
        _track_unmapped_category(what, unmapped_categories)
        story_id = story["story_id"]
        if story_id in seen:
            duplicates += 1
            continue
        seen.add(story_id)
        stories.append(story)

    oldest = "unknown"
    if activities:
        when = activities[-1].get("when")
        if isinstance(when, int):
            oldest = datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%d %H:%M")

    logs.append(
        {
            "level": "info",
            "message": (
                f"Activity wall scanned: {len(activities)} activities back to {oldest} UTC,"
                f" {publications} story publication(s), {len(stories)} kept"
                f" ({duplicates} duplicate(s) dropped)"
            ),
        }
    )

    if warning := _unmapped_categories_log(unmapped_categories):
        logs.append(warning)

    if not stories:
        logs.append(
            {
                "level": "warning",
                "message": (
                    "The Literotica activity wall held no story publications -"
                    " check that you follow authors on literotica.com"
                ),
            }
        )

    ctx.report(100.0)

    return stories, logs
