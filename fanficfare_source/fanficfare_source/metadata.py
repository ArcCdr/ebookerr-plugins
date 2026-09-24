"""FanFicFare-JSON → core fields mapping — owned by the FanFicFare source plugin half, not the core.

Field names/types are grounded in the captured fixtures (``tests/fixtures/README.md``):
FanFicFare emits camelCase keys with string-typed numerics (``numChapters:"1"``,
``numWords:""``) and HTML descriptions. This module is pure (no I/O).

This is the FanFicFare-owned slice of the pull's books-table column map. For the full
column-by-column provenance — including the non-FanFicFare columns the core orchestrator
fills in (packaged chapter count, provider-sync fields, timing columns) — see
``SourcePullService``'s class docstring (``src/services/source_pull_service.py``).
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.domain.ids import make_book_id

# FanFicFare JSON key -> Book text field. Empty strings become None.
_TEXT_FIELDS: dict[str, str] = {
    "title": "title",
    "author": "author",
    "authorUrl": "author_url",
    "storyUrl": "story_url",
    "sectionUrl": "section_url",
    "series": "series",
    "seriesUrl": "series_url",
    "description": "description",
    "category": "category",
    "eroticatags": "erotica_tags",
    "status": "status",
    "site": "site",
    "cover_image": "cover_image",
    "output_filename": "output_filename",
    "storyId": "story_id",
}

# FanFicFare JSON key -> Book datetime field. Parsed at this frontier; never stored as text.
_DATE_FIELDS: dict[str, str] = {
    "datePublished": "date_published",
    "dateUpdated": "date_updated",
}

# Human-readable text columns sanitised at persistence (tag-strip + entity-decode).
# URL columns are intentionally excluded: they feed make_book_id and html.unescape
# would corrupt a URL containing a literal &amp; query parameter.
_SANITISE_COLUMNS: frozenset[str] = frozenset(
    {
        "title",
        "author",
        "series",
        "description",
        "category",
        "erotica_tags",
        "status",
        "site",
    }
)

# FanFicFare JSON key -> Book integer field (string-typed source, may be empty).
_INT_FIELDS: dict[str, str] = {
    "numChapters": "num_chapters",
    "numWords": "num_words",
}


def _str_or_none(value: Any) -> str | None:
    """Stringify and strip ``value``.

    ``None``/empty/whitespace-only becomes ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    """Parse ``value`` as an int, stripping thousands separators.

    Unparsable or empty input returns ``None``.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


_TAG_RE = re.compile(r"<[^>]+>")


def sanitize(value: str | None) -> str | None:
    """F3 field sanitiser: strip HTML tags, decode entities, trim the ends.

    Applied to every local/FanFicFare string field before it is compared against or
    submitted to Komga (ARCHITECTURE.md §F3). Internal whitespace is preserved; only the
    ends are stripped. ``None`` (and any non-string) passes through unchanged.
    """
    if not isinstance(value, str):
        return value
    return html.unescape(_TAG_RE.sub("", value)).strip()


def fanficfare_json_to_book_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Map a FanFicFare metadata payload to Book constructor kwargs (no ``book_id``).

    Human-readable text columns are sanitised (HTML-stripped, entity-decoded) at
    persistence. URL columns are left verbatim — they feed make_book_id and must
    never be entity-decoded. No ``fanficfare_json`` key is emitted: the raw payload is
    no longer archived anywhere (the ``books.fanficfare_json`` blob column was dropped,
    migration step 28) — only the fields/chapters derived here are persisted.
    ``datePublished``/``dateUpdated`` are parsed into timezone-aware UTC datetimes here
    — the parse frontier for FanFicFare payloads.
    """
    fields: dict[str, Any] = {col: _str_or_none(data.get(key)) for key, col in _TEXT_FIELDS.items()}
    fields.update({col: _int_or_none(data.get(key)) for key, col in _INT_FIELDS.items()})
    fields.update({col: parse_datetime(data.get(key)) for key, col in _DATE_FIELDS.items()})
    for col in _SANITISE_COLUMNS:
        fields[col] = sanitize(fields[col])
    # Map language if present and non-empty
    language = _str_or_none(data.get("language"))
    if language:
        fields["language"] = language
    return fields


def fanficfare_book_id(data: Mapping[str, Any]) -> str | None:
    """Canonical book id from a FanFicFare payload (the same URL rule as the upsert).

    FanFicFare normalises the requested URL to the site's canonical story/section URL
    (e.g. an individual-part URL becomes the series URL), so ids derived here follow
    FanFicFare's identity, never the user's input. ``None`` when the payload has no URL.
    """
    story_url = _str_or_none(data.get("storyUrl"))
    section_url = _str_or_none(data.get("sectionUrl"))
    if not story_url and not section_url:
        return None
    return make_book_id(story_url, section_url)


def chapter_urls_from_fanficfare(json_data: Mapping[str, Any]) -> list[str]:
    """Ordered, de-duplicated chapter URLs from FanFicFare's ``zchapters`` metadata.

    ``zchapters`` is a list of ``[number, {"title": ..., "url": ...}]`` pairs. Returns ``[]``
    on missing/malformed input — never raises.
    """
    zchapters = json_data.get("zchapters")
    if not isinstance(zchapters, list):
        return []

    seen: set[str] = set()
    urls: list[str] = []

    for entry in zchapters:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        chapter_meta = entry[1]
        if not isinstance(chapter_meta, dict):
            continue
        url = chapter_meta.get("url")
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            urls.append(url)

    return urls


def chapter_links_from_fanficfare(
    json_data: Mapping[str, Any],
) -> list[tuple[str, str | None, int]]:
    """Extract (url, title, ordinal) triples from zchapters, deduped by URL keeping first.

    Returns a list of (url, title, ordinal) tuples where title is the entry's "title" field
    (a string) or None if absent, and ordinal is the 1-based position in the chapter table.
    Deduplication keeps the first occurrence of each URL.
    """
    zchapters = json_data.get("zchapters")
    if not isinstance(zchapters, list):
        return []

    seen: set[str] = set()
    links: list[tuple[str, str | None, int]] = []

    for ordinal, entry in enumerate(zchapters, start=1):
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        chapter_meta = entry[1]
        if not isinstance(chapter_meta, dict):
            continue
        url = chapter_meta.get("url")
        if not (isinstance(url, str) and url):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = chapter_meta.get("title")
        if not isinstance(title, str):
            title = None
        links.append((url, title, ordinal))

    return links
