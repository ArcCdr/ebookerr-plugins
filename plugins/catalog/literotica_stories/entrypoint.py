#!/usr/bin/env python3
"""Literotica search catalog plugin - pure API mapping functions."""

from __future__ import annotations

import contextlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import cast


class CatalogHostUnreachable(Exception):  # noqa: N818
    """The host's circuit breaker is open, so this scan made no request at all."""


def _report_progress(percent: float) -> None:
    """Emit a fire-and-forget progress frame on stdout (percent in 0-100).

    Args:
        percent: Progress percentage in [0, 100].
    """
    print(json.dumps({"op": "progress", "percent": percent}), flush=True)  # noqa: T201


API_URL = "https://literotica.com/api/3/search/stories"
PAGE_SIZE = 50
_UA = "Mozilla/5.0 (ebookerr LiteroticaStories catalog plugin)"
_TIMEOUT_S = 30
_DELAY_S = 1.0
_RETRY_DELAY_S = 5.0
CIRCUIT_KEY = "host:literotica.com"
CIRCUIT_LABEL = "literotica.com"
_LANGUAGE_NAMES = {1: "en"}
_TYPE_PREFIX = {"story": "s", "audio": "s", "poem": "p", "illustration": "i"}
# Literotica's own category display names, keyed by the integer id the search API returns in
# each story's `category` field. The API exposes no display name anywhere - `category_info`
# carries only a URL slug, and title-casing that slug produced internal-looking values
# ("Non Consent Stories") that disagreed with both the site and FanFicFare
# ("Reluctance/NonConsent"). Shared verbatim with the sibling `my_literotica` catalog plugin;
# exec plugins run as separate processes and cannot import a shared module.
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


def _parse_int_list(value: str) -> list[int]:
    """Parse comma-separated integers, skipping invalid tokens."""
    result = []
    for token in value.split(","):
        with contextlib.suppress(ValueError):
            result.append(int(token))
    return result


def _parse_string_list(value: str) -> list[str]:
    """Parse comma-separated strings, stripping and dropping blanks."""
    return [t.strip() for t in value.split(",") if t.strip()]


def _get_tags_string(tags_list: list) -> str:
    """Extract and join tags from a list of tag dicts.

    A tag's ``tag`` value is usually a string, but the upstream API sends at least one
    slang tag ("69") as a raw JSON number; stringify rather than crash on it.
    """
    tags = []
    for t in tags_list:
        if isinstance(t, dict) and t.get("tag"):
            tags.append(str(t["tag"]))
    return ", ".join(tags)


def _get_series_info(series: object) -> tuple[str | None, str | None]:
    """Extract series name and URL from a series dict."""
    if not isinstance(series, dict):
        return None, None
    meta = series.get("meta", {})
    title = meta.get("title") or None
    url = f"https://www.literotica.com/series/se/{meta['id']}" if meta.get("id") else None
    return title, url


def category_name(raw: dict) -> str | None:
    """Return Literotica's own display name for a story's category, or None when unmapped.

    Args:
        raw: A story dict from the API response.

    Returns:
        The mapped display name, or ``None`` when ``category`` is missing, non-numeric, or
        an id Literotica added after this table was written.
    """
    try:
        return _CATEGORY_NAMES[int(raw["category"])]
    except (KeyError, TypeError, ValueError):
        return None


def category_label(raw: dict) -> str | None:
    """Display label for a story's category: Literotica's own name, else the slug title-cased.

    The fallback keeps an unmapped category readable rather than dropping it, which matters
    because Literotica adds categories without notice.

    Args:
        raw: A story dict from the API response.

    Returns:
        The category label, or ``None`` when the payload carries neither a mapped id nor a
        URL slug.
    """
    mapped = category_name(raw)
    if mapped is not None:
        return mapped
    slug = (raw.get("category_info") or {}).get("pageUrl")
    if not slug:
        return None
    return " ".join(word.capitalize() for word in str(slug).split("-"))


def _add_optional_fields(patch: dict, raw: dict) -> None:
    """Add optional string fields to patch from raw story."""
    if raw.get("title"):
        patch["title"] = raw["title"]
    if raw.get("description"):
        patch["description"] = raw["description"]
    if (rating := raw.get("rate_all")) is not None:
        patch["rating"] = rating
    if (words := raw.get("words_count")) is not None:
        patch["num_words"] = words
    if (sid := raw.get("id")) is not None:
        patch["story_id"] = str(sid)


def _build_custom_fields(raw: dict) -> dict:
    """Extract custom fields from raw story dict."""
    custom = {}
    for key, field in [
        ("Votes", "rate_count"),
        ("Views", "view_count"),
        ("Favorites", "favorite_count"),
        ("Comments", "comment_count"),
        ("Rank", "rank"),
        ("Reading Lists", "reading_lists_count"),
    ]:
        if (v := raw.get(field)) is not None:
            custom[key] = v
    if raw.get("language") is not None:
        custom["Language"] = _LANGUAGE_NAMES.get(raw["language"], str(raw["language"]))
    if raw.get("type"):
        custom["Type"] = str(raw["type"])
    for key, field in [
        ("Is Hot", "is_hot"),
        ("Is New", "is_new"),
        ("Writer's Pick", "writers_pick"),
        ("Contest Winner", "contest_winner"),
        ("Downloadable", "allow_download"),
    ]:
        if field in raw:
            custom[key] = bool(raw[field])
    if "series_count" in raw:
        custom["Series Parts"] = raw["series_count"]
    if (v := raw.get("author", {}).get("stories_count")) is not None:
        custom["Author Stories"] = v
    return custom


def parse_search_url(url: str) -> tuple[dict, int]:
    """Parse a search.literotica.com URL into API params and start_page.

    Args:
        url: A URL like https://search.literotica.com/?query=a&page=2&...

    Returns:
        A tuple of (api_params_dict, start_page_int).
        api_params_dict contains normalized query parameters for the API.
        start_page_int is extracted from the 'page' parameter (default 1).
    """
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    start_page = 1
    if "page" in params:
        with contextlib.suppress(ValueError, IndexError):
            start_page = int(params["page"][0])

    api_params: dict = {}
    api_params["q"] = params.get("query", [""])[0]

    if "categories" in params:
        cats = _parse_int_list(params["categories"][0])
        if cats:
            api_params["categories"] = cats

    if "tags" in params:
        tags = _parse_string_list(params["tags"][0])
        if tags:
            api_params["tags"] = tags

    if "period" in params:
        api_params["period"] = params["period"][0]

    if "popular" in params:
        api_params["popular"] = params["popular"][0].lower() in ("true", "1")

    if "sort" in params:
        api_params["sort"] = params["sort"][0]

    langs = _parse_int_list(params["languages"][0]) if "languages" in params else []
    api_params["languages"] = langs if langs else [1]

    return api_params, start_page


def unknown_params(url: str) -> list[str]:
    """Return sorted list of query parameter keys not recognized by parse_search_url.

    Args:
        url: A URL like https://search.literotica.com/?query=a&foo=1&bar=2

    Returns:
        Sorted list of unknown query key names.
    """
    known = {"query", "page", "categories", "tags", "period", "popular", "sort", "languages"}
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    unknown = [k for k in params if k not in known]
    return sorted(unknown)


def build_api_url(params: dict, page: int) -> str:
    """Build a Literotica API URL with compact JSON params.

    Args:
        params: API parameters dict (e.g., {"q": "a", "languages": [1]})
        page: Page number to request

    Returns:
        Full URL string for the API request.
    """
    params_with_page = {**params, "page": page}
    json_str = json.dumps(params_with_page, separators=(",", ":"))
    encoded = urllib.parse.quote(json_str)
    return f"{API_URL}?params={encoded}"


def story_url(story_type: object, slug: str) -> str:
    """Build the canonical Literotica URL for a content slug.

    Literotica serves each content kind under its own prefix - stories and audio under ``/s/``,
    poems under ``/p/``, illustrations under ``/i/``. An unknown or missing type falls back to
    ``/s/``, which Literotica redirects to the right prefix.

    Args:
        story_type: The raw payload's ``type`` value.
        slug: The raw payload's ``url`` slug.

    Returns:
        The absolute story URL.
    """
    prefix = _TYPE_PREFIX.get(str(story_type), "s")
    return f"https://www.literotica.com/{prefix}/{slug}"


def map_story(raw: dict) -> dict | None:
    """Map a raw Literotica API story object to a StoryPatch dict.

    Every patch declares ``metadata_fetched=True``: a listing row already carries the full
    metadata the site offers, so the core never offers it to a metadata pass (SPI 2.11).

    Args:
        raw: A story dict from the API response

    Returns:
        A StoryPatch dict, or None if the story has no URL.
    """
    # Skip stories without a URL
    if not raw.get("url"):
        return None

    patch: dict = {}

    # url (required)
    patch["url"] = story_url(raw.get("type"), raw["url"])
    patch["metadata_fetched"] = True
    _add_optional_fields(patch, raw)

    # author and author_url
    username = raw.get("author", {}).get("username") or raw.get("authorname")
    if username:
        patch["author"] = username
        patch["author_url"] = f"https://www.literotica.com/authors/{username}"

    # category - Literotica's own display name, so the Stories page and the library agree
    if category := category_label(raw):
        patch["category"] = category

    # tags (comma-separated from list)
    if tags_str := _get_tags_string(raw.get("tags", [])):
        patch["tags"] = tags_str

    # date_published
    try:
        date_obj = datetime.strptime(raw["date_approve"], "%m/%d/%Y")  # noqa: DTZ007
        patch["date_published"] = date_obj.date().isoformat()
    except (KeyError, ValueError, TypeError):
        pass

    # series
    if series_name := _get_series_info(raw.get("series"))[0]:
        patch["series"] = series_name
    if series_url := _get_series_info(raw.get("series"))[1]:
        patch["series_url"] = series_url

    # site
    patch["site"] = "literotica.com"

    # custom fields
    custom = _build_custom_fields(raw)
    if custom:
        patch["custom"] = custom

    return patch


def _meets_threshold(story: dict, name: str, minval: float) -> bool:
    """Check if a story meets a single threshold condition."""
    value = story[name] if name in story else story.get("custom", {}).get(name)

    if value is None:
        return False

    if isinstance(value, bool):
        value_float = float(value)
    elif isinstance(value, str):
        return False
    else:
        try:
            value_float = float(value)
        except (ValueError, TypeError):
            return False

    return value_float >= minval


def apply_thresholds(stories: list[dict], thresholds: dict[str, float]) -> list[dict]:
    """Filter stories by thresholds.

    Keep a story only if ALL threshold conditions pass.
    A threshold condition fails if the value is missing/None, or cannot coerce to float >= minval.

    Args:
        stories: List of StoryPatch dicts
        thresholds: Dict of {field_name: minimum_value}

    Returns:
        Filtered list of stories.
    """
    if not thresholds:
        return stories

    result = []
    for story in stories:
        if all(_meets_threshold(story, name, minval) for name, minval in thresholds.items()):
            result.append(story)

    return result


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
            {
                "op": "circuit",
                "call": call,
                "key": CIRCUIT_KEY,
                "label": CIRCUIT_LABEL,
                "ok": ok,
            }
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


def fetch_json(url: str) -> dict[str, object]:
    """Fetch and parse JSON from a URL, guarded by the host's circuit breaker.

    Asks the core once before spending a request budget: a host the app has already found
    unreachable is not asked again (EXP-269). Every attempt's outcome is reported back,
    so the breaker opens on a run of transport failures and closes on the first success. An
    HTTP status is **never** a breaker failure — a 404 is absence and a 401 is
    misconfiguration, and neither means the host stopped answering.

    Raises:
        CatalogHostUnreachable: When the breaker is open, before any request is made.
    """
    if _circuit("is_open"):
        raise CatalogHostUnreachable(CIRCUIT_LABEL)
    for attempt in range(2):
        try:
            request = urllib.request.Request(  # noqa: S310
                url, headers={"User-Agent": _UA, "Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
                body = cast(dict[str, object], json.loads(response.read().decode("utf-8")))
            _circuit("record", ok=True)
            return body
        except urllib.error.HTTPError as e:
            # The server answered. Absence and misconfiguration are not an outage, so the
            # breaker is told the call succeeded — the resilience4j/pybreaker
            # ignore-exceptions rule (EXP-269).
            _circuit("record", ok=True)
            if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(_RETRY_DELAY_S)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            open_now = _circuit("record", ok=False)
            if open_now:
                raise CatalogHostUnreachable(CIRCUIT_LABEL) from None
            if attempt == 0:
                time.sleep(_RETRY_DELAY_S)
                continue
            raise
    msg = "fetch_json: unreachable"
    raise RuntimeError(msg)


def _track_unmapped_categories(items: list, unmapped_categories: set[tuple[str, str]]) -> None:
    """Record any category id in a fetched page that has no entry in ``_CATEGORY_NAMES``.

    Args:
        items: Raw story dicts from one fetched page.
        unmapped_categories: Set of (category id, category slug) pairs, updated in place.
    """
    for item in items:
        if isinstance(item, dict) and category_name(item) is None:
            slug = (item.get("category_info") or {}).get("pageUrl")
            if slug:
                unmapped_categories.add((str(item.get("category")), str(slug)))


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


def scan(settings: dict) -> tuple[list[dict], list[dict]]:  # noqa: C901
    """Scan multiple search URLs for stories with pagination and thresholds.

    Returns:
        Tuple of (stories_list, logs_list) where logs entries are
        {"level": str, "message": str} dicts.

    Raises:
        RuntimeError: On any page-fetch failure (EXP-073, EXP-227); a plugin that fetched
            only some of its pages or URLs raises rather than returning a short listing.

    Note:
        Complexity is unavoidably high due to dual paging loops and distinct exception
        handlers for transport failures (EXP-269) vs. HTTP/parse errors (EXP-073).
    """
    logs: list[dict] = []
    all_stories: list[dict] = []
    seen_urls: set[str] = set()
    unmapped_categories: set[tuple[str, str]] = set()

    urls = settings.get("search_urls") or []
    max_pages = max(1, int(settings.get("max_pages") or 1))
    thresholds = settings.get("min_thresholds") or {}

    first_request = True
    total = len(urls)

    for index, url in enumerate(urls):
        # Warn on unknown params
        unknown = unknown_params(url)
        if unknown:
            logs.append(
                {
                    "level": "warning",
                    "message": f"{url}: ignoring unknown search parameters: {', '.join(unknown)}",
                }
            )

        # Parse URL to get start page and params
        params, start_page = parse_search_url(url)

        url_stories: list[dict] = []
        pages_fetched = 0

        # Page through results
        for page in range(start_page, start_page + max_pages):
            # Sleep before every request except the very first one of the whole scan
            if not first_request:
                time.sleep(_DELAY_S)
            first_request = False

            try:
                api_url = build_api_url(params, page)
                data = fetch_json(api_url)
                items = data.get("data") or []

                # Map stories, dropping None results
                mapped = [map_story(raw) for raw in items]
                url_stories.extend([s for s in mapped if s is not None])

                # Aggregate, never per story: a page of 50 unmapped stories must produce one line.
                _track_unmapped_categories(items, unmapped_categories)

                pages_fetched += 1
                logs.append(
                    {
                        "level": "debug",
                        "message": f"{url}: page {page}: {len(items)} stories",
                    }
                )

                # Stop paging if this page was short
                if len(items) < PAGE_SIZE:
                    break

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
                break
            except Exception as e:
                logs.append(
                    {
                        "level": "error",
                        "message": f"scan failed for {url} page {page}: {e}",
                    }
                )
                raise RuntimeError(f"scan failed for {url} page {page}: {e}") from e

        # Apply thresholds
        kept = apply_thresholds(url_stories, thresholds)

        logs.append(
            {
                "level": "info",
                "message": (
                    f"{url}: fetched {pages_fetched} page(s), {len(url_stories)} stories, "
                    f"kept {len(kept)} after thresholds"
                ),
            }
        )

        # Add kept stories, deduplicating by URL
        for story in kept:
            story_url = story.get("url")
            if story_url and story_url not in seen_urls:
                all_stories.append(story)
                seen_urls.add(story_url)

        _report_progress((index + 1) / total * 100.0 if total else 100.0)

    if warning := _unmapped_categories_log(unmapped_categories):
        logs.append(warning)

    logs.append(
        {
            "level": "info",
            "message": f"Scan complete: {len(all_stories)} unique stories from {len(urls)} URL(s)",
        }
    )

    return all_stories, logs


def main() -> None:
    """Read scan request from stdin, execute scan, output JSON response."""
    logs: list[dict] = []

    try:
        request = json.loads(input())
        op = request.get("op")

        if op != "scan":
            raise ValueError(f"unsupported operation: {op}")

        settings = request.get("request", {}).get("settings", {})
        result, scan_logs = scan(settings)
        logs.extend(scan_logs)

        output = {"ok": True, "result": result, "logs": logs}
        print(json.dumps(output))  # noqa: T201

    except Exception as exc:
        output = {"ok": False, "error": str(exc), "logs": logs}
        print(json.dumps(output))  # noqa: T201
        sys.exit(0)


if __name__ == "__main__":
    main()
