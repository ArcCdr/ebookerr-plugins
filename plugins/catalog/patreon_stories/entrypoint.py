#!/usr/bin/env python3
"""Patreon Stories catalog plugin - real API client with parsing functions."""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

API_BASE = "https://www.patreon.com/api"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Every membership the signed-in account holds: paid, cancelled but still entitled, free, or
# lapsed. The legacy `pledges` include returns an empty list for all of them (measured 2026-09-11).
MEMBERSHIPS_URL = (
    API_BASE + "/current_user?include=active_memberships.campaign.creator"
    "&fields[member]=patron_status,pledge_relationship_end,access_expires_at"
    "&fields[campaign]=name,url&fields[user]=full_name&json-api-version=1.0"
)
POSTS_URL_TEMPLATE = (
    API_BASE + "/posts?filter[campaign_id]={campaign_id}&sort=-published_at"
    "&include=attachments_section,media"
    "&fields[post]=title,url,published_at,post_file,post_type,current_user_can_view,content"
    "&json-api-version=1.0&page[count]=20"
)

_TIMEOUT_S = 30
# A campaign scan stops after this many posts pages (20 posts each), even before the cutoff.
_MAX_POSTS_PAGES = 20

# Keyed by the **host**, never by this plugin: the process-wide registry gives every caller
# naming this host one breaker and one outage (EXP-269, R3). Exec plugins run as separate
# processes and cannot import a shared module, so this helper is duplicated verbatim in each
# sidecar that reaches the network.
CIRCUIT_KEY = "host:patreon.com"
CIRCUIT_LABEL = "patreon.com"


class CatalogHostUnreachable(Exception):  # noqa: N818
    """The host's circuit breaker is open, so this scan made no request at all."""


def _circuit(call: str, *, ok: bool = True) -> bool:
    """Ask the core about this host's circuit breaker over the stdio channel (``SPI 2.21``).

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


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_RTF_MIMES = {"application/rtf", "text/rtf"}
_TEXT_MIMES = {"text/plain", "text/markdown", "text/x-markdown"}

# Set up logging to suppress verbose third-party loggers
logging.getLogger("urllib3").setLevel(logging.WARNING)


def _report_progress(percent: float) -> None:
    """Emit a fire-and-forget progress frame on stdout (percent in 0-100).

    Args:
        percent: Progress percentage in [0, 100].
    """
    print(json.dumps({"op": "progress", "percent": percent}), flush=True)  # noqa: T201


def _get_json(url: str, session_cookie: str, cookie_name: str = "session_id") -> dict[str, object]:
    """Fetch and parse JSON from Patreon API with auth cookie.

    Args:
        url: The API endpoint URL
        session_cookie: Patreon session cookie value
        cookie_name: Name of the session cookie, as configured in the site-auth
            profile (defaults to "session_id" — the documented convention —
            when the profile predates this field or omits it)

    Returns:
        Parsed JSON dict

    Raises:
        RuntimeError: On HTTP 401/403 (auth error) or other HTTP errors
        CatalogHostUnreachable: When the host circuit breaker is open or a transport error occurs
    """
    if _circuit("is_open"):
        raise CatalogHostUnreachable(CIRCUIT_LABEL)

    try:
        request = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "Cookie": f"{cookie_name}={session_cookie}",
                "User-Agent": _UA,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            _circuit("record", ok=True)
            return cast(dict[str, object], json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        _circuit("record", ok=True)
        if e.code in (401, 403):
            msg = "Patreon session cookie missing or expired"
            raise RuntimeError(msg) from e
        msg = f"Patreon API error: HTTP {e.code}"
        raise RuntimeError(msg) from e
    except (urllib.error.URLError, TimeoutError) as e:
        _circuit("record", ok=False)
        raise CatalogHostUnreachable(CIRCUIT_LABEL) from e


def parse_campaigns(
    memberships_json: dict[str, object], logs: list[dict[str, object]] | None = None
) -> list[dict[str, object]]:
    """Parse the campaigns of the signed-in account's memberships.

    Walks the JSON-API ``included`` section: every ``campaign`` object becomes one campaign, its
    creator's name comes from the ``user`` its ``creator`` relationship points at, and its
    membership state from the ``member`` object whose ``campaign`` relationship points at it (see
    ``membership_label``). A campaign missing its name, URL or creator name is skipped and reported
    as one warning entry appended to ``logs``.

    Args:
        memberships_json: The JSON-API response from the memberships endpoint.
        logs: The scan's exec-log list, bridged into the app log; a skipped campaign appends one
            warning entry. None drops the warning (callers outside a scan, such as the live
            contract tests).

    Returns:
        List of campaign dicts with keys: campaign_id, name, url, creator_name, membership (the
        ``membership_label`` string, or None when the campaign has no known membership).
    """
    included: Any = memberships_json.get("included", [])

    # Build lookup maps: users and campaigns by id, membership attributes by campaign id.
    users_by_id: dict[str, Any] = {}
    campaigns_by_id: dict[str, Any] = {}
    member_attrs_by_campaign: dict[str, Any] = {}
    for item in included:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "user":
            users_by_id[item["id"]] = item
        elif kind == "campaign":
            campaigns_by_id[item["id"]] = item
        elif kind == "member":
            campaign_ref: Any = item.get("relationships", {}).get("campaign", {}).get("data")
            if isinstance(campaign_ref, dict) and campaign_ref.get("id"):
                member_attrs_by_campaign[campaign_ref["id"]] = item.get("attributes")

    campaigns: list[dict[str, object]] = []
    for campaign in campaigns_by_id.values():
        campaign_id = campaign.get("id")
        attrs: Any = campaign.get("attributes", {})
        name = attrs.get("name")
        url = attrs.get("url")

        # Get creator info from relationships
        rels: Any = campaign.get("relationships", {})
        creator_data: Any = rels.get("creator", {}).get("data", {})
        creator_id = creator_data.get("id") if isinstance(creator_data, dict) else None
        creator_user = users_by_id.get(creator_id, {}) if creator_id else {}
        creator_name = (
            creator_user.get("attributes", {}).get("full_name")
            if isinstance(creator_user, dict)
            else None
        )

        # Validate required fields; a skipped campaign is reported, never silently dropped.
        if not all([campaign_id, name, url, creator_name]):
            if logs is not None:
                logs.append(
                    {
                        "level": "warning",
                        "message": (
                            f"Skipped Patreon campaign campaign_id={campaign_id}:"
                            " missing its name, URL or creator name"
                        ),
                    }
                )
            continue

        campaigns.append(
            {
                "campaign_id": campaign_id,
                "name": name,
                "url": url,
                "creator_name": creator_name,
                "membership": membership_label(member_attrs_by_campaign.get(campaign_id)),
            }
        )

    return campaigns


def is_authenticated(memberships_json: dict[str, object]) -> bool:
    """Return whether the memberships response describes a signed-in Patreon user.

    Patreon answers a refused session cookie with HTTP 200 and a payload carrying no user, which
    is otherwise indistinguishable from a signed-in account with no memberships. The signed-in
    shape always carries ``data.id``.

    Args:
        memberships_json: The JSON-API response from the memberships endpoint.

    Returns:
        ``True`` when the payload carries a user object with a non-empty ``id``.
    """
    data: Any = memberships_json.get("data")
    if not isinstance(data, dict):
        return False
    return bool(data.get("id"))


# Month abbreviations for the membership label, spelled out so a label never depends on the locale.
_MONTH_ABBREVIATIONS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def membership_label(member_attrs: object) -> str | None:
    """Describe a Patreon membership the way its member sees it.

    Patreon keeps a cancelled paid membership as ``patron_status="active_patron"`` until the paid
    period ends; what marks it cancelled is a set ``pledge_relationship_end``, and
    ``access_expires_at`` says until when its posts stay unlocked. A free membership has
    ``patron_status`` null, and a lapsed paid one is ``former_patron``.

    Args:
        member_attrs: The ``attributes`` of a JSON-API ``member`` object, or None when the
            campaign has no membership object.

    Returns:
        ``"Active"``, ``"Cancelled (access until 17 Sep 2026)"`` (``"Cancelled"`` when Patreon
        reports no usable end of access), ``"Payment declined"``, ``"Former"`` or ``"Free"``; None
        when there is no membership object, it carries no ``patron_status`` key, or the status is
        one this plugin does not know.
    """
    if not isinstance(member_attrs, dict) or "patron_status" not in member_attrs:
        return None
    status = member_attrs.get("patron_status")
    if status is None:
        return "Free"
    if status == "active_patron":
        if not member_attrs.get("pledge_relationship_end"):
            return "Active"
        until = _format_access_date(member_attrs.get("access_expires_at"))
        return f"Cancelled (access until {until})" if until else "Cancelled"
    if status == "declined_patron":
        return "Payment declined"
    if status == "former_patron":
        return "Former"
    return None


def _format_access_date(value: object) -> str | None:
    """Format a Patreon ISO-8601 timestamp as ``"17 Sep 2026"``.

    Args:
        value: The raw ``access_expires_at`` attribute.

    Returns:
        The day, abbreviated month and year, or None when the value is absent or unparseable.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{moment.day} {_MONTH_ABBREVIATIONS[moment.month - 1]} {moment.year}"


def _collect_media_ids(post: dict[str, object]) -> list[str]:
    """Collect media IDs from a post's relationships."""
    media_ids_ordered: list[str] = []
    seen_ids: set[str] = set()
    rels: Any = post.get("relationships", {})
    for rel_name in ["attachments_section", "media"]:
        rel_data: Any = rels.get(rel_name, {}).get("data", [])
        if isinstance(rel_data, dict):
            rel_data = [rel_data]
        for item in rel_data:
            if isinstance(item, dict):
                media_id = item.get("id")
                if media_id and media_id not in seen_ids:
                    media_ids_ordered.append(media_id)
                    seen_ids.add(media_id)
    return media_ids_ordered


def _build_media_list(media_ids: list[str], media_by_id: dict[str, Any]) -> list[dict[str, object]]:
    """Build media list from IDs."""
    media_list: list[dict[str, object]] = []
    for media_id in media_ids:
        media_item: Any = media_by_id.get(media_id, {})
        media_attrs: Any = media_item.get("attributes", {})
        media_list.append(
            {
                "media_id": media_id,
                "file_name": media_attrs.get("file_name", ""),
                "mimetype": media_attrs.get("mimetype", ""),
                "download_url": media_attrs.get("download_url", ""),
            }
        )
    return media_list


def parse_posts_page(
    page_json: dict[str, object],
) -> tuple[list[dict[str, object]], str | None]:
    """Parse a posts API page response.

    Args:
        page_json: The JSON-API response from the posts endpoint

    Returns:
        Tuple of (posts_list, next_cursor).
        Each post dict has keys: post_id, title, published_at, post_type, can_view,
        content, media (list of {media_id, file_name, mimetype, download_url})
    """
    posts: list[dict[str, object]] = []
    included: Any = page_json.get("included", [])

    # Build media lookup
    media_by_id: dict[str, Any] = {}
    for item in included:
        if isinstance(item, dict) and item.get("type") == "media":
            media_by_id[item["id"]] = item

    # Process each post in data
    data: Any = page_json.get("data", [])
    for post in data:
        post_id = post.get("id")
        attrs: Any = post.get("attributes", {})

        # Collect media from both attachments_section and media relationships
        media_ids = _collect_media_ids(cast(dict[str, object], post))
        media_list = _build_media_list(media_ids, media_by_id)

        posts.append(
            {
                "post_id": post_id,
                "title": attrs.get("title", ""),
                "published_at": attrs.get("published_at", ""),
                "post_type": attrs.get("post_type", ""),
                "can_view": bool(attrs.get("current_user_can_view", False)),
                "content": attrs.get("content") or "",
                "media": media_list,
            }
        )

    # Extract next cursor
    meta: Any = page_json.get("meta", {})
    pagination: Any = meta.get("pagination", {})
    cursors: Any = pagination.get("cursors", {})
    next_cursor = cursors.get("next")
    if not next_cursor:
        next_cursor = None

    return posts, next_cursor


def _mime_to_format(mimetype: str) -> str | None:
    """Map MIME type to story format string.

    Args:
        mimetype: The MIME type to map

    Returns:
        Format string (epub/pdf/docx/rtf/txt) or None if unsupported
    """
    if mimetype == "application/epub+zip":
        return "epub"
    if mimetype == "application/pdf":
        return "pdf"
    if mimetype == _DOCX_MIME:
        return "docx"
    if mimetype in _RTF_MIMES:
        return "rtf"
    if mimetype in _TEXT_MIMES:
        return "txt"
    return None


def select_story_media(
    media: list[dict[str, object]], prefer: str | None = None
) -> dict[str, object] | None:
    """Select the best media file for download.

    Collects first candidate for each format among media with non-empty download_url.
    Default preference order: epub > docx > pdf > rtf > txt.
    With prefer, returns that format's candidate (or None if absent).

    Args:
        media: List of media dicts with mimetype and download_url
        prefer: Optional format preference ("epub", "docx", "pdf", "rtf", "txt").
            If None (default), uses preference order.

    Returns:
        The selected media dict, or None if no suitable file found
    """
    candidates: dict[str, dict[str, object]] = {}

    for item in media:
        mimetype = item.get("mimetype", "")
        download_url = item.get("download_url", "")

        if not download_url:
            continue

        fmt = _mime_to_format(str(mimetype))
        if fmt and fmt not in candidates:
            candidates[fmt] = item

    # If prefer is set, return only that format
    if prefer is not None:
        return candidates.get(prefer)

    # Default preference order
    preference_order = ("epub", "docx", "pdf", "rtf", "txt")
    for fmt in preference_order:
        if fmt in candidates:
            return candidates[fmt]

    return None


def _plain_excerpt(html: str, limit: int) -> str | None:
    """Strip HTML tags, collapse whitespace, and truncate.

    Args:
        html: HTML string to process
        limit: Maximum character count before truncation

    Returns:
        Cleaned excerpt with ellipsis if truncated, or None if empty
    """
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = " ".join(text.split())

    if not text:
        return None

    # Truncate at word boundary
    if len(text) <= limit:
        return text

    truncated = text[:limit]
    # Find last space to break on word boundary
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]

    return truncated + "…"


def build_story(
    post: dict[str, object], campaign: dict[str, object], media: dict[str, object]
) -> dict[str, object]:
    """Build a StoryPatch wire dict from a post and media.

    Every patch declares ``metadata_fetched=True``: a listing row already carries the full
    metadata the site offers, so the core never offers it to a metadata pass (SPI 2.11).
    The campaign's membership state, when known, becomes the ``Membership`` custom label
    (e.g. ``"Free"``).

    Args:
        post: Post dict from parse_posts_page
        campaign: Campaign dict from parse_campaigns
        media: Selected media dict from select_story_media

    Returns:
        StoryPatch wire dict
    """
    post_id = post["post_id"]
    media_id = media["media_id"]
    file_name = media["file_name"]
    mimetype = media["mimetype"]

    # Determine format from mimetype
    format_str = _mime_to_format(str(mimetype)) or "epub"

    # Title fallback to media file stem
    title_val: Any = post.get("title")
    title_str = title_val if isinstance(title_val, str) else ""
    file_name_str = file_name if isinstance(file_name, str) else ""
    title = title_str or Path(file_name_str).stem

    # Build excerpt from content
    content: Any = post.get("content", "")
    content_str = content if isinstance(content, str) else ""
    description = _plain_excerpt(content_str, 500)

    custom: dict[str, object] = {
        "Post Type": post["post_type"],
        "Campaign": campaign["name"],
    }
    membership = campaign.get("membership")
    if isinstance(membership, str) and membership:
        custom["Membership"] = membership
    if file_name_str:
        custom["source_filename"] = file_name_str

    return {
        "url": f"https://www.patreon.com/file?h={post_id}&m={media_id}",
        "format": format_str,
        "title": title,
        "author": campaign["creator_name"],
        "author_url": campaign["url"],
        "site": "patreon.com",
        "date_published": post["published_at"],
        "story_id": str(post_id),
        "series": campaign["name"],
        "description": description,
        "custom": custom,
        "metadata_fetched": True,
    }


def _process_post(
    post: dict[str, object],
    campaign: dict[str, object],
    cutoff: datetime,
    seen_urls: set[str],
) -> tuple[dict[str, object] | None, int, int]:
    """Process a single post, return (story or None, locked_count, no_file_count)."""
    published_at_str: Any = post.get("published_at", "")
    if not isinstance(published_at_str, str):
        published_at_str = ""

    if published_at_str:
        try:
            iso_str = published_at_str.replace("Z", "+00:00")
            published_at = datetime.fromisoformat(iso_str)
            if published_at < cutoff:
                return None, 0, 0
        except ValueError:
            pass

    if not post["can_view"]:
        return None, 1, 0

    post_media: Any = post.get("media", [])
    media = select_story_media(post_media if isinstance(post_media, list) else [])
    if not media:
        return None, 0, 1

    story = build_story(post, campaign, media)
    story_url = story.get("url")
    if story_url and story_url not in seen_urls:
        seen_urls.add(str(story_url))
        return story, 0, 0

    return None, 0, 0


def _scan_campaign(
    campaign: dict[str, object],
    session_cookie: str,
    cutoff: datetime,
    seen_urls: set[str],
    cookie_name: str = "session_id",
) -> tuple[list[dict[str, object]], int, int, int, bool]:
    """Scan a single campaign for stories.

    Returns: (stories, kept_count, skipped_locked_count, skipped_no_file_count, truncated) —
    ``truncated`` is True when the scan stopped at ``_MAX_POSTS_PAGES`` pages before reaching
    the look-back cutoff.
    """
    stories: list[dict[str, object]] = []
    kept = 0
    skipped_locked = 0
    skipped_no_file = 0
    truncated = False

    posts_url_template = POSTS_URL_TEMPLATE.format(campaign_id=campaign["campaign_id"])
    next_cursor = None
    page_count = 0

    while True:
        page_count += 1
        if page_count > _MAX_POSTS_PAGES:
            truncated = True
            break

        if next_cursor:
            posts_url = posts_url_template + "&page[cursor]=" + urllib.parse.quote(next_cursor)
        else:
            posts_url = posts_url_template

        posts_data = _get_json(posts_url, session_cookie, cookie_name)
        posts, next_cursor = parse_posts_page(posts_data)

        cutoff_hit = False
        for post in posts:
            story, locked, no_file = _process_post(post, campaign, cutoff, seen_urls)
            skipped_locked += locked
            skipped_no_file += no_file

            if story:
                stories.append(story)
                kept += 1
            elif locked == 0 and no_file == 0 and post.get("published_at"):
                try:
                    pub_str = cast(str, post.get("published_at", ""))
                    iso_str = pub_str.replace("Z", "+00:00")
                    if datetime.fromisoformat(iso_str) < cutoff:
                        cutoff_hit = True
                        break
                except ValueError:
                    pass

        if cutoff_hit or not next_cursor:
            break

    return stories, kept, skipped_locked, skipped_no_file, truncated


_MEMBERSHIP_STATES = ("Active", "Cancelled", "Payment declined", "Former", "Free")


def _membership_summary(campaigns: list[dict[str, object]]) -> str:
    """Summarise the discovered memberships by state, for the scan log.

    Args:
        campaigns: Campaign dicts from ``parse_campaigns``.

    Returns:
        E.g. ``"Found 3 Patreon membership(s): 0 active, 1 cancelled, 0 payment declined,
        1 former, 1 free, 0 unknown"``.
    """
    counts = dict.fromkeys(_MEMBERSHIP_STATES, 0)
    unknown = 0
    for campaign in campaigns:
        label = campaign.get("membership")
        # "Cancelled (access until 17 Sep 2026)" counts as "Cancelled".
        state = label.split(" (", 1)[0] if isinstance(label, str) else None
        if state in counts:
            counts[state] += 1
        else:
            unknown += 1
    parts = ", ".join(f"{counts[state]} {state.lower()}" for state in _MEMBERSHIP_STATES)
    return f"Found {len(campaigns)} Patreon membership(s): {parts}, {unknown} unknown"


def scan(
    request_auth: dict[str, object],
    settings: dict[str, object] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Scan every Patreon membership the account holds for posts it can open.

    Paid, cancelled-but-still-entitled, free and lapsed memberships are all scanned; whether a
    post becomes a story is decided per post by Patreon's ``current_user_can_view``, never by
    the membership's state.

    Args:
        request_auth: Auth dict from request.auth with patreon.com credentials
        settings: Settings dict with optional 'recent_weeks' key
        now_fn: Callable returning current datetime (for testing); defaults to datetime.now(UTC)

    Returns:
        Tuple of (stories_list, logs_list)

    Raises:
        RuntimeError: When the session cookie is missing, when Patreon does not accept it, or
            when any memberships/posts fetch fails — an empty result always means "the source
            really has nothing" (EXP-073, EXP-227). A signed-in account with no memberships is
            such an empty result, not a failure (EXP-245).
    """
    logs: list[dict[str, object]] = []
    stories: list[dict[str, object]] = []

    if settings is None:
        settings = {}

    if now_fn is None:

        def default_now() -> datetime:
            """Return the current UTC time."""
            return datetime.now(UTC)

        now_fn = default_now

    # Extract session cookie from auth config
    patreon_auth: Any = request_auth.get("patreon.com", {})
    session_cookie_val: Any = (
        patreon_auth.get("value", "") if isinstance(patreon_auth, dict) else ""
    )
    session_cookie = session_cookie_val if isinstance(session_cookie_val, str) else ""
    if not session_cookie:
        msg = (
            "Patreon session cookie not configured — add a patreon.com profile"
            " under Settings → Site authentication"
        )
        raise RuntimeError(msg)

    # The site-auth profile's own cookie name — must match what SiteAuthService.headers_for
    # sends for the same profile when EpubDownloadSourcePlugin later downloads the story.
    cookie_name_val: Any = patreon_auth.get("name", "") if isinstance(patreon_auth, dict) else ""
    cookie_name = (
        cookie_name_val if isinstance(cookie_name_val, str) and cookie_name_val else "session_id"
    )

    # Calculate cutoff time based on recent_weeks
    recent_weeks_val: Any = settings.get("recent_weeks")
    recent_weeks = int(recent_weeks_val) if recent_weeks_val else 4
    now = now_fn()
    cutoff = now - timedelta(weeks=recent_weeks)

    # Fetch every membership and parse its campaign
    memberships_data = _get_json(MEMBERSHIPS_URL, session_cookie, cookie_name)
    campaigns = parse_campaigns(memberships_data, logs)

    if not campaigns:
        # An empty listing means "the source really has nothing", and only that (EXP-073,
        # EXP-227). Zero memberships on a signed-in account is the normal state of a Patreon
        # account, so it returns an empty listing and says so at INFO. A refused session cookie
        # also yields zero campaigns, so it is told apart by the payload and still raises.
        if not is_authenticated(memberships_data):
            msg = (
                "Patreon did not accept the stored session cookie — refresh it under"
                " Settings → Site authentication"
            )
            raise RuntimeError(msg)
        logs.append(
            {
                "level": "info",
                "message": "No Patreon memberships found for this account",
            }
        )
        _report_progress(100.0)
        return [], logs

    # One summary line of what was discovered, by membership state.
    logs.append({"level": "info", "message": _membership_summary(campaigns)})

    # Track seen URLs for deduplication
    seen_urls: set[str] = set()

    # For each campaign, fetch posts and build stories
    total = len(campaigns)
    for index, campaign in enumerate(campaigns):
        campaign_name = campaign["name"]
        campaign_id = campaign["campaign_id"]

        try:
            camp_stories, kept, skipped_locked, skipped_no_file, truncated = _scan_campaign(
                campaign, session_cookie, cutoff, seen_urls, cookie_name
            )
            stories.extend(camp_stories)

            # Log campaign summary
            membership = campaign.get("membership") or "unknown"
            log_msg = (
                f'Scanned campaign "{campaign_name}" (campaign_id={campaign_id},'
                f" membership={membership}): {kept} stories kept,"
                f" {skipped_locked} locked posts skipped,"
                f" {skipped_no_file} posts without a supported file"
            )
            logs.append({"level": "info", "message": log_msg})
            if truncated:
                warning_msg = (
                    f'Stopped scanning campaign "{campaign_name}" (campaign_id={campaign_id})'
                    f" after {_MAX_POSTS_PAGES} pages: older posts were not checked against"
                    f" the {recent_weeks}-week look-back window"
                )
                logs.append({"level": "warning", "message": warning_msg})

        except RuntimeError as e:
            error_msg = f'Campaign "{campaign_name}" (campaign_id={campaign_id}): scan failed: {e}'
            logs.append({"level": "error", "message": error_msg})
            raise RuntimeError(error_msg) from e

        _report_progress((index + 1) / total * 100.0 if total else 100.0)

    return stories, logs


def main() -> None:
    """Read scan request from stdin, execute scan, output JSON response."""
    logs: list[dict[str, object]] = []

    try:
        request = json.loads(input())
        op = request.get("op")

        if op != "scan":
            raise ValueError(f"unsupported operation: {op}")

        request_auth = request.get("request", {}).get("auth", {})
        request_settings = request.get("request", {}).get("settings", {})
        result, scan_logs = scan(request_auth, request_settings)
        logs.extend(scan_logs)

        output = {"ok": True, "result": result, "logs": logs}
        print(json.dumps(output))  # noqa: T201

    except CatalogHostUnreachable:
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
