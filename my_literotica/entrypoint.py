#!/usr/bin/env python3
"""My Literotica catalog plugin - the authenticated activity wall as StoryPatches."""

from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

# Shared verbatim with the sibling `literotica_stories` catalog plugin; exec plugins run as
# separate processes and cannot import a shared module. The key is the **host**, so both
# plugins share one breaker and one outage (EXP-269, R3).
CIRCUIT_KEY = "host:literotica.com"
CIRCUIT_LABEL = "literotica.com"


class CatalogHostUnreachable(Exception):  # noqa: N818
    """The host's circuit breaker is open, so this scan made no request at all."""


def _report_progress(percent: float) -> None:
    """Emit a fire-and-forget progress frame on stdout (percent in 0-100).

    Args:
        percent: Progress percentage in [0, 100].
    """
    print(json.dumps({"op": "progress", "percent": percent}), flush=True)  # noqa: T201


def _circuit(call: str, *, ok: bool = True) -> bool:
    """Ask the core about this host's circuit breaker over the stdio channel (SPI 2.21).

    A sidecar is one process per invocation, so it can hold no breaker state of its own; the
    core owns a process-wide registry and answers here. The key names the **host**, not this
    plugin, so the sibling catalog that fetches the same site shares one breaker and one
    outage rather than each paying it (EXP-269).

    Args:
        call: ``"is_open"`` to ask, or ``"record"`` to report a call's outcome.
        ok: For ``"record"``, whether the call this plugin just made succeeded.

    Returns:
        True when the breaker is refusing calls. A broken or unanswered channel returns
        False, so a transport problem can never stop a scan that would otherwise work.
    """
    print(  # noqa: T201
        json.dumps(
            {"op": "circuit", "call": call, "key": CIRCUIT_KEY, "label": CIRCUIT_LABEL, "ok": ok}
        ),
        flush=True,
    )
    try:
        line = sys.stdin.readline()
    except OSError:
        return False
    if not line:
        return False
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    verdict = obj.get("circuit")
    return bool(verdict.get("open", False)) if isinstance(verdict, dict) else False


API_BASE = "https://literotica.com/api/3"
WALL_URL = API_BASE + "/activity/wall"
AUTH_LOGIN_URL = (
    "https://auth.literotica.com/login?redirect=www.literotica.com"
    "&err_redirect=https%3A%2F%2Fwww.literotica.com%2Fauthenticate%2Flogin"
)
AUTH_CHECK_URL = "https://auth.literotica.com/check"
SITE_HOST = "literotica.com"
SITE_BASE = "https://www.literotica.com"
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
# plugin; exec plugins run as separate processes and cannot import a shared module.
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
        slug: The ``what.url`` slug, e.g. ``"bimbo-potion-the-aftermath"``.

    Returns:
        The absolute story URL.
    """
    prefix = _TYPE_PREFIX.get(str(story_type), "s")
    return f"{SITE_BASE}/{prefix}/{slug}"


def build_custom_fields(what: dict, when: object) -> dict:  # noqa: C901
    """Extract the Stories-page custom columns from one wall payload.

    Labels are shared verbatim with the sibling ``literotica_stories`` catalog plugin wherever
    the two report the same value, so both catalogs contribute the same columns.

    Args:
        what: The activity's ``what`` object.
        when: The activity's ``when`` unix timestamp.

    Returns:
        A label-keyed mapping of ``str``/``int``/``float``/``bool`` values.
    """
    custom: dict = {}

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


def _add_optional_fields(patch: dict, what: dict) -> None:
    """Add title, description, rating and word count to the patch when present."""
    if what.get("title"):
        patch["title"] = what["title"]
    if what.get("description"):
        patch["description"] = what["description"]
    if what.get("rate_all") is not None:
        patch["rating"] = what["rate_all"]
    if what.get("words_count") is not None:
        patch["num_words"] = what["words_count"]


def _add_author_fields(patch: dict, what: dict) -> None:
    """Add the author name and profile URL, falling back to the flat ``authorname``."""
    author = what.get("author")
    username = (author.get("username") if isinstance(author, dict) else None) or what.get(
        "authorname"
    )
    if username:
        patch["author"] = username
        patch["author_url"] = f"{SITE_BASE}/authors/{username}"


def category_name(what: dict) -> str | None:
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


def category_label(what: dict) -> str | None:
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


def _add_category_and_tags(patch: dict, what: dict) -> None:
    """Add Literotica's own category display name and the comma-joined tag list."""
    category = category_label(what)
    if category:
        patch["category"] = category

    tags = [str(t["tag"]) for t in what.get("tags", []) if isinstance(t, dict) and t.get("tag")]
    if tags:
        patch["tags"] = ", ".join(tags)


def _add_date_published(patch: dict, what: dict) -> None:
    """Add ``date_published`` in ISO form, parsed from the MM/DD/YYYY ``date_approve`` field.

    Emits ISO so both Literotica catalogs agree byte for byte; a missing or unparseable
    ``date_approve`` leaves the field unset rather than raising.
    """
    try:
        approved = datetime.strptime(what["date_approve"], "%m/%d/%Y")  # noqa: DTZ007
        patch["date_published"] = approved.date().isoformat()
    except (KeyError, TypeError, ValueError):
        pass


def _add_series_fields(patch: dict, what: dict) -> None:
    """Add the series title and URL when the activity belongs to a series."""
    series = what.get("series")
    if isinstance(series, dict):
        meta = series.get("meta") or {}
        if meta.get("title"):
            patch["series"] = meta["title"]
        if meta.get("id"):
            patch["series_url"] = f"{SITE_BASE}/series/se/{meta['id']}"


def map_activity(activity: dict) -> dict | None:
    """Map one activity-wall entry to a StoryPatch wire dict.

    Only ``published-story`` activities produce a patch; every other activity kind, and any
    malformed payload, returns ``None``.
    Every patch declares ``metadata_fetched=True``: a listing row already carries the full
    metadata the site offers, so the core never offers it to a metadata pass (SPI 2.11).

    Args:
        activity: One entry from the wall response's ``data`` list.

    Returns:
        The StoryPatch wire dict, or ``None`` when the activity is not a usable publication.
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

    patch: dict = {
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


def fetch_wall_page(token: str, last_id: str | None) -> list[dict]:
    """Fetch one page of the activity wall.

    Args:
        token: The bearer token.
        last_id: The previous page's last activity ``id``, or ``None`` for the first page.

    Returns:
        The page's ``data`` list (empty when the wall is exhausted).

    Raises:
        CatalogHostUnreachable: When the host's circuit breaker is open.
        urllib.error.HTTPError: If the wall rejects the token or returns an error status.
        urllib.error.URLError: If the wall could not be reached (transport error).
        TimeoutError: If the request times out.
        RuntimeError: If the response is unparseable JSON.
    """
    if _circuit("is_open"):
        raise CatalogHostUnreachable(CIRCUIT_LABEL)

    params: dict = {"chunked": 1, "limit": PAGE_SIZE}
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
        _circuit("record", ok=True)
        raise
    except (urllib.error.URLError, TimeoutError):
        open_now = _circuit("record", ok=False)
        if open_now:
            raise CatalogHostUnreachable(CIRCUIT_LABEL) from None
        raise
    except json.JSONDecodeError as exc:
        msg = "Literotica activity wall returned malformed JSON"
        raise RuntimeError(msg) from exc

    _circuit("record", ok=True)
    data = payload.get("data")
    return data if isinstance(data, list) else []


def fetch_activities(token: str) -> tuple[list[dict], list[dict]]:
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

    Returns:
        A tuple of (activities, log entries).
    """
    logs: list[dict] = []
    activities: list[dict] = []
    seen_ids: set[str] = set()
    last_id: str | None = None

    for page in range(1, MAX_PAGES + 1):
        items = fetch_wall_page(token, last_id)
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


def mint_token(sessionid: str) -> str:
    """Exchange a Literotica ``sessionid`` cookie for a fresh one-hour bearer token.

    The auth service answers with the raw JWT as the response body (not JSON).

    Args:
        sessionid: The ``sessionid`` cookie value obtained from :func:`login`.

    Returns:
        The bearer token, without the ``Bearer `` prefix.

    Raises:
        CatalogHostUnreachable: When the host's circuit breaker is open.
        urllib.error.HTTPError: If the session is rejected.
        urllib.error.URLError: If the auth service could not be reached (transport error).
        TimeoutError: If the request times out.
        RuntimeError: If the response body is not a valid JWT.
    """
    if _circuit("is_open"):
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
        _circuit("record", ok=True)
        raise
    except (urllib.error.URLError, TimeoutError):
        open_now = _circuit("record", ok=False)
        if open_now:
            raise CatalogHostUnreachable(CIRCUIT_LABEL) from None
        raise

    _circuit("record", ok=True)
    if token.count(".") != 2:
        msg = "Literotica token refresh returned an unexpected response"
        raise RuntimeError(msg)
    return token


def resolve_token(request_auth: dict) -> tuple[str, bool]:
    """Turn the stored literotica.com site-auth profile into a wall bearer token.

    Two profile shapes are supported: ``basic`` (username + password, which logs in and mints a
    fresh one-hour token on every scan) and a ``cookie`` named ``auth_token`` (a token pasted
    from the browser, which expires within the hour).

    Args:
        request_auth: The request envelope's ``auth`` mapping, keyed by host.

    Returns:
        A tuple of (bearer token, ``True`` when the token was freshly minted from credentials).

    Raises:
        RuntimeError: If no literotica.com profile is stored or its shape is unsupported.
    """
    profile = request_auth.get(SITE_HOST)
    if not isinstance(profile, dict) or not profile.get("value"):
        msg = (
            "Literotica credentials not configured - add a literotica.com profile"
            " under Settings -> Site authentication"
        )
        raise RuntimeError(msg)

    kind = profile.get("kind", "")
    name = profile.get("name", "")
    value = profile["value"]

    if kind == "basic":
        return mint_token(login(str(name), str(value))), True
    if kind == "cookie" and name == "auth_token":
        return str(value), False

    msg = (
        "Literotica profile must be kind 'basic' (username + password) or a cookie"
        " named 'auth_token' - see Settings -> Site authentication"
    )
    raise RuntimeError(msg)


def _track_unmapped_category(what: dict, unmapped_categories: set[tuple[str, str]]) -> None:
    """Record an activity's category id if it has no entry in ``_CATEGORY_NAMES``.

    Args:
        what: One activity's ``what`` mapping.
        unmapped_categories: Set of (category id, category slug) pairs, updated in place.
    """
    if category_name(what) is None:
        slug = (what.get("category_info") or {}).get("pageUrl")
        if slug:
            unmapped_categories.add((str(what.get("category")), str(slug)))


def _unmapped_categories_log(unmapped_categories: set[tuple[str, str]]) -> dict | None:
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


def scan(request_auth: dict) -> tuple[list[dict], list[dict]]:
    """Scan the Literotica activity wall and return every story publication it holds.

    The endpoint exposes only the 200 most recent activities, so the whole wall is drained on
    every scan; there is no look-back setting to honour.

    Args:
        request_auth: The request envelope's ``auth`` mapping, keyed by host.

    Returns:
        A tuple of (StoryPatch wire dicts, log entries).

    Raises:
        RuntimeError: No usable credential is stored, the wall rejected the token, or the wall
            could not be fetched. Raised — never reported as an empty wall — so the core fails
            the scan and keeps the stored catalog (standing rule, EXP-073).
    """
    logs: list[dict] = []

    token, minted = resolve_token(request_auth)

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

    activities, fetch_logs = fetch_activities(token)
    logs.extend(fetch_logs)

    _report_progress(50.0)

    stories: list[dict] = []
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

    _report_progress(100.0)

    return stories, logs


def main() -> None:
    """Read the scan request from stdin, run the scan, print the JSON response."""
    logs: list[dict] = []

    try:
        request = json.loads(input())
        op = request.get("op")

        if op != "scan":
            raise ValueError(f"unsupported operation: {op}")

        request_auth = request.get("request", {}).get("auth", {})
        result, scan_logs = scan(request_auth)
        logs.extend(scan_logs)

        output = {"ok": True, "result": result, "logs": logs}
        print(json.dumps(output))  # noqa: T201

    except CatalogHostUnreachable:
        # Host is unreachable due to open circuit breaker; return success with warning.
        logs.append(
            {
                "level": "warning",
                "message": (
                    f"{CIRCUIT_LABEL} is not reachable, so this scan made no request. "
                    "It will be retried automatically."
                ),
            }
        )
        output = {"ok": True, "result": [], "logs": logs}
        print(json.dumps(output))  # noqa: T201

    except Exception as exc:
        output = {"ok": False, "error": str(exc), "logs": logs}
        print(json.dumps(output))  # noqa: T201
        sys.exit(0)


if __name__ == "__main__":
    main()
