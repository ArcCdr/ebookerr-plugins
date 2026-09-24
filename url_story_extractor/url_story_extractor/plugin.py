"""Catch-all story extractor: lists stories on any page FanFicFare can read.

The extraction mirror of ``FanFicFareSourcePlugin``: ``extract_url_patterns=("^https?://",)``
at ``priority=1000`` makes it the floor beneath every more specific extractor (``EXT-D3``), so
a future site-specialised catalog registered at a lower priority wins its own URLs with no
core change.

``scan`` covers every URL in the ``extract_urls`` setting (a full snapshot); ``extract_stories``
answers one ad-hoc URL (merged in, never pruning); ``enrich_stories`` enriches metadata for
already-extracted URLs without re-listing their pages.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ebookerr_sdk.domain.chapter_number import extract_chapter_info
from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.domain.ids import normalise_url, site_key
from ebookerr_sdk.domain.story_url import (
    author_from_listing_url,
    author_page_url_from_listing_url,
    derive_story_identity,
    title_from_url,
)
from ebookerr_sdk.spi import (
    KNOWN_URLS_KEY,
    PluginContext,
    PluginManifest,
    PluginType,
    SettingsField,
    SettingsSchema,
    StoryPatch,
    decode_known_urls,
)

from url_story_extractor.pages import FanFicFarePagesGateway

__all__ = ["UrlStoryExtractorPlugin", "default_pages"]

logger = logging.getLogger(__name__)

_MANIFEST = PluginManifest(
    id="url_story_extractor",
    name="URL Story Extractor",
    version="1.1.0",
    plugin_type=PluginType.CATALOG,
    settings_schema=SettingsSchema(
        fields=(
            SettingsField(
                key="extract_urls",
                type="url_list",
                label="Watched URLs",
                default=[],
                help=(
                    "Listing pages re-scanned on every catalog refresh - an author's works page, "
                    "a series page, a favourites list. Add one from the Stories page with "
                    '"Keep watching" ticked.'
                ),
            ),
            SettingsField(
                key="max_new_metadata_per_scan",
                type="int",
                label="Metadata fetches per scan",
                default="25",
                help=(
                    "How many newly-found stories may have their full metadata fetched in one "
                    "scan. A story already listed is never re-fetched."
                ),
            ),
            SettingsField(
                key="request_delay_ms",
                type="int",
                label="Delay between metadata fetches (ms)",
                default="750",
                help="Spacing between metadata requests, to stay a polite visitor.",
            ),
        ),
        summary=(
            "{extract_urls|No watched URLs} watched URL(s) · "
            "{max_new_metadata_per_scan} metadata lookup(s) per scan"
        ),
    ),
    headless=True,
    priority=1000,
    network=True,
    extract_url_patterns=(r"^https?://",),
    default_enabled=True,
    run_timeout_s=1800,
    icon="travel_explore",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/url_story_extractor",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/url_story_extractor",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    requirements=("fanficfare>=4.58.1",),
    description=(
        "Extracts the stories listed on any page — an author's works, a series, a favourites "
        "list — using FanFicFare's site adapters, falling back to generic link scraping."
    ),
)


@dataclass
class _MetadataBudget:
    """One scan's allowance of metadata fetches, shared across every URL that scan covers.

    ``EXT-FR-10`` caps metadata fetches **per scan**, not per listing page: a user watching ten
    author pages must not pay ten times the configured cap. ``spaced`` makes the inter-request
    delay a property of the scan too, so the first fetch of a second listing page waits like any
    other rather than firing immediately.
    """

    remaining: int
    spaced: bool = False


def default_pages() -> FanFicFarePagesGateway:
    """Build the page gateway on the FanFicFare configuration this install uses (D37).

    The FanFicFare Source owns the one ``personal.ini`` holding the user's site logins, at
    ``<plugin data root>/fanficfare_source/personal.ini``; when it exists this plugin reads it, otherwise
    it reads its own packaged default beside this module.

    Returns:
        A gateway reading the shared configuration file, or the packaged default.
    """
    data_dir = os.environ.get("EBOOKERR_PLUGIN_DATA_DIR")
    if data_dir:
        shared = Path(data_dir).parent / "fanficfare_source" / "personal.ini"
        if shared.is_file():
            logger.debug("Reading FanFicFare configuration from %s", shared)
            return FanFicFarePagesGateway(shared)
    packaged = Path(__file__).with_name("personal.ini")
    logger.debug("Reading the packaged FanFicFare configuration %s", packaged)
    return FanFicFarePagesGateway(packaged)


def _cap(ctx: PluginContext) -> int:
    """Return the per-scan metadata-fetch cap from settings, or 0 when unset.

    Args:
        ctx: The plugin context.

    Returns:
        The configured cap, or 0 if unset or empty.
    """
    return int(ctx.settings.get("max_new_metadata_per_scan") or 0)


def _delay_ms(ctx: PluginContext) -> int:
    """Return the inter-request delay in milliseconds from settings, or 0 when unset.

    Args:
        ctx: The plugin context.

    Returns:
        The configured delay in milliseconds, or 0 if unset or empty.
    """
    return int(ctx.settings.get("request_delay_ms") or 0)


def _patch_for(story_url: str, *, listing_url: str) -> StoryPatch | None:
    """Build the URL-only StoryPatch for story_url, or None when its URL is unusable.

    Args:
        story_url: The story URL to build a patch for.
        listing_url: The listing page URL the story was extracted from.

    Returns:
        A StoryPatch with url, title, author, author_url, and site set as derived from the URLs;
        story_id is not set. The author and author_url are derived from the listing URL
        (or None if the listing URL is not a recognised author page). Returns None if the
        story_url has no derivable identity.
    """
    if not derive_story_identity(story_url):
        return None

    title = title_from_url(story_url) or story_url
    author = author_from_listing_url(listing_url) or None
    author_url = author_page_url_from_listing_url(listing_url) or None
    site = site_key(story_url) or None

    return StoryPatch(
        url=story_url,
        title=title,
        author=author,
        author_url=author_url,
        site=site,
    )


# Digit-group separators a site may put in a count: comma, ASCII space, non-breaking space,
# narrow no-break space, thin space. Everything else fails the int() and is dropped as before.
_DIGIT_NOISE_RE = re.compile(r"[,\s   ]")


def _clean_text(value: Any) -> str:
    """Return *value* as a stripped string with HTML entities decoded.

    Args:
        value: The value to clean (typically a string from metadata).

    Returns:
        A string with HTML entities unescaped and leading/trailing whitespace removed.
    """
    return html.unescape(str(value)).strip()


def _to_int(value: Any) -> int | None:
    """Return *value* as an int, tolerating digit-group separators, or None.

    Args:
        value: The value to parse (typically a count from metadata).

    Returns:
        An integer with digit-group separators removed, or None if parsing fails.
    """
    try:
        return int(_DIGIT_NOISE_RE.sub("", str(value)))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    """Return *value* as a float, or ``None`` when it is not a number.

    Args:
        value: The value to parse (typically a rating from metadata).

    Returns:
        A float, or None if parsing fails (e.g., ``"4.72"`` → ``4.72``).
    """
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# Fields a PARENT work's metadata may still assert about one of its parts: who wrote it, where to
# find them, and how the site files it. Everything else — the title, the dates, the counts, the
# status, the blurb — belongs to the parent and would be a lie on the part's row.
_PART_TEXT_FIELDS = {
    "author": "author",
    "authorUrl": "author_url",
    "category": "category",
    "genre": "tags",
}

# Fields a story's OWN metadata asserts about itself.
_WHOLE_TEXT_FIELDS = {
    "title": "title",
    "author": "author",
    "authorUrl": "author_url",
    "series": "series",
    "seriesUrl": "series_url",
    "description": "description",
    "category": "category",
    "genre": "tags",
    "status": "status",
}


def _enrich_part_metadata(
    patch: StoryPatch, meta: dict[str, Any], changes: dict[str, Any]
) -> StoryPatch:
    """Enrich a chapter row with parent work metadata.

    Records the parent's title as the series and parent's URL as series_url, without
    copying numeric or dated fields that belong to the parent.

    Args:
        patch: The chapter row being enriched.
        meta: The parent work's metadata.
        changes: The field changes dict to update.

    Returns:
        The updated patch.
    """
    parent_title = _clean_text(meta.get("title", ""))
    if parent_title:
        changes["series"] = parent_title
    raw_parent = meta.get("storyUrl")
    if (
        isinstance(raw_parent, str)
        and raw_parent
        and normalise_url(raw_parent) != normalise_url(patch.url)
    ):
        changes["series_url"] = raw_parent
    return replace(patch, **changes)


def _is_part(story_url: str, url_title: str, meta: Mapping[str, Any]) -> bool:
    """Decide whether metadata describes a story's parent work, not the story itself.

    Uses two deterministic rules to detect when fetched metadata names a larger work
    that the story URL is one part of. The decision is structural, not probabilistic:
    it reads the site's own canonical URL first, and only falls back to the
    title/chapter-count comparison when the site offers no distinct canonical URL.

    Rule 1 — the site names a different canonical URL:
    If meta["storyUrl"] is a non-empty string and normalise_url(meta["storyUrl"])
    differs from normalise_url(story_url), the adapter resolved this URL to a parent,
    so the metadata is the parent's. Return True.

    Rule 2 — the URL names a chapter the metadata title does not:
    If all four hold:
    - normalise_url(meta.get("storyUrl", story_url)) == normalise_url(story_url)
      (Rule 1 did not fire)
    - _to_int(meta.get("numChapters")) is not None and is > 1
    - extract_chapter_info(url_title).chapter_number != ""
    - extract_chapter_info(_clean_text(meta.get("title", ""))).chapter_number == ""

    The URL identifies one chapter, the metadata title names a multi-chapter work and
    carries no chapter number of its own — so the metadata is the parent's. Return True.

    Otherwise return False.

    Args:
        story_url: The story URL that metadata was fetched for.
        url_title: The title derived from the story URL.
        meta: The raw FanFicFare metadata dict.

    Returns:
        True if metadata describes a parent work, False if it describes the story itself.
    """
    raw = meta.get("storyUrl")
    if isinstance(raw, str) and raw and normalise_url(raw) != normalise_url(story_url):
        return True

    chapters = _to_int(meta.get("numChapters"))
    if chapters is None or chapters <= 1:
        return False

    if not extract_chapter_info(url_title).chapter_number:
        return False

    return not extract_chapter_info(_clean_text(meta.get("title", ""))).chapter_number


def _apply_metadata(patch: StoryPatch, meta: dict[str, Any]) -> StoryPatch:
    """Apply FanFicFare metadata to a URL-only patch, enriching non-empty fields.

    Metadata may describe either the story itself or a parent work that the story is part of.
    The signal is whether meta["storyUrl"] differs from the story's own URL: if so, the
    metadata names the parent, and only certain fields are copied (author, category, tags).
    Everything else — title, dates, counts, status, description — would misrepresent the
    chapter row, and stays untouched. The parent's title becomes the series instead.

    For a story's own metadata (same URL), all supported fields are applied: title, author,
    authorUrl→author_url, series, seriesUrl→series_url, description, category, genre→tags,
    status, numChapters→num_chapters, numWords→num_words, datePublished→date_published,
    dateUpdated→date_updated, averrating→rating. Text values are HTML-unescaped and stripped;
    those that result in empty strings are dropped. Counts tolerate digit-group separators
    (commas, spaces). Empty values and parsing failures are dropped; existing patch fields are
    never blanked.

    Args:
        patch: The URL-only StoryPatch to enrich.
        meta: The raw FanFicFare metadata dict.

    Returns:
        A new StoryPatch with enriched fields, or the original if meta has no usable values.
    """
    part = _is_part(patch.url, patch.title or "", meta)
    changes: dict[str, Any] = {}
    text_fields = _PART_TEXT_FIELDS if part else _WHOLE_TEXT_FIELDS

    for raw_key, patch_field in text_fields.items():
        if (raw_value := meta.get(raw_key)) and (cleaned := _clean_text(raw_value)):
            changes[patch_field] = cleaned

    if part:
        return _enrich_part_metadata(patch, meta, changes)

    changes.update(_whole_story_numeric_fields(meta))
    return replace(patch, **changes)


def _whole_story_numeric_fields(meta: dict[str, Any]) -> dict[str, Any]:
    """Parse a whole story's numeric, date and rating fields out of raw metadata.

    Split out of :func:`_apply_metadata` to keep that function's branching within the
    project's complexity gate; behaviour is unchanged. Rating is FanFicFare's site
    average (``averrating``, "4.72" on Literotica) — whole stories only, since whether a
    part inherits its parent's rating is an ``EXT-D20`` question and stays out of scope.

    Args:
        meta: The raw FanFicFare metadata dict.

    Returns:
        A dict of patch field names to parsed values; a field with no usable value is omitted.
    """
    changes: dict[str, Any] = {}

    for raw_key, patch_field in [("numChapters", "num_chapters"), ("numWords", "num_words")]:
        if raw_key in meta:
            parsed = _to_int(meta[raw_key])
            if parsed is not None:
                changes[patch_field] = parsed

    for raw_key, patch_field in [
        ("datePublished", "date_published"),
        ("dateUpdated", "date_updated"),
    ]:
        if raw_key in meta:
            dt = parse_datetime(meta[raw_key])
            if dt is not None:
                changes[patch_field] = dt

    if "averrating" in meta:
        rating = _to_float(meta["averrating"])
        if rating is not None:
            changes["rating"] = rating

    return changes


def _propagate_author(patches: list[StoryPatch]) -> tuple[list[StoryPatch], int, str]:
    """Apply a listing's unanimous author to rows fetched from its URL alone.

    Collects ``_clean_text(p.author)`` from every patch with ``p.metadata_fetched is True``
    and a non-empty ``author``. If the set of distinct values is not exactly one, returns
    the patches unchanged with count=0 and author="". A favourites listing (author page
    whose stories are by other people) must never be relabelled — unanimity is required
    to prevent mislabelling a mixed listing.

    With exactly one distinct author, takes that author and the ``author_url`` of the
    **first** fetched patch carrying that author and a non-empty ``author_url`` (or
    ``None`` when none does). Replaces ``author`` and ``author_url`` on every patch
    whose ``metadata_fetched`` is ``False``, in place in the list order, and counts them.

    A patch with ``metadata_fetched is True`` is never modified — the site's own answer
    always wins.

    Args:
        patches: The patches to process, in order.

    Returns:
        A tuple of (patches, applied_count, author), where:
        - patches: The list in the same order, with modifications applied in place.
        - applied_count: How many un-fetched patches were updated.
        - author: The confirmed author string, or "" if unanimity failed.
    """
    if not patches:
        return [], 0, ""

    # Collect distinct authors from fetched rows only
    fetched_authors = set()
    for patch in patches:
        if patch.metadata_fetched and patch.author:
            fetched_authors.add(_clean_text(patch.author))

    # Unanimity is required
    if len(fetched_authors) != 1:
        return patches, 0, ""

    confirmed_author = fetched_authors.pop()

    # Find the first fetched row with this author and a non-empty author_url
    confirmed_author_url = None
    for patch in patches:
        if (
            patch.metadata_fetched
            and patch.author
            and _clean_text(patch.author) == confirmed_author
            and patch.author_url
        ):
            confirmed_author_url = patch.author_url
            break

    # Apply to un-fetched rows
    applied_count = 0
    result = []
    for patch in patches:
        if not patch.metadata_fetched:
            result.append(
                replace(
                    patch,
                    author=confirmed_author,
                    author_url=confirmed_author_url,
                )
            )
            applied_count += 1
        else:
            result.append(patch)

    return result, applied_count, confirmed_author


class UrlStoryExtractorPlugin:
    """Catch-all story extractor: lists the stories on any page FanFicFare can read.

    The extraction mirror of ``FanFicFareSourcePlugin``: ``extract_url_patterns=("^https?://",)``
    at ``priority=1000`` makes it the floor beneath every more specific extractor (``EXT-D3``), so
    a future site-specialised catalog registered at a lower priority wins its own URLs with no
    core change.

    ``scan`` covers every URL in the ``extract_urls`` setting (a full snapshot); ``extract_stories``
    answers one ad-hoc URL (merged in, never pruning); ``enrich_stories`` enriches metadata for
    already-extracted URLs without re-listing their pages.
    """

    manifest = _MANIFEST

    def __init__(self, *, pages: FanFicFarePagesGateway | None = None) -> None:
        """Store the injected gateway and compile the manifest's claim patterns.

        Args:
            pages: The gateway used to list story URLs and fetch metadata; built by
                :func:`default_pages` when omitted (the plugin process serves one call).
        """
        self._pages = pages if pages is not None else default_pages()
        # One source of truth: the claim patterns are the manifest's, compiled once, exactly as
        # ExecCatalogPlugin does for a local_exec catalog (EXT-TR-4).
        patterns = self.manifest.extract_url_patterns
        self._patterns = tuple(re.compile(pattern) for pattern in patterns)

    def _patches_for_listing(self, url: str, ctx: PluginContext) -> list[StoryPatch]:
        """List *url* and build one URL-only StoryPatch per story found.

        No metadata is fetched here — that is the second, budgeted tier (``EXT-D12``). A story URL
        with no derivable identity is dropped and logged at DEBUG; a listing that yields nothing
        is a WARNING, because the user pointed at this page on purpose.

        Raises: ListingError: propagated from the gateway — a scan that cannot read a watched page
            fails as a whole, so the results store is never written (standing rule, EXP-073).

        Args:
            url: The listing page URL.
            ctx: Runtime services for this call.

        Returns:
            Zero or more URL-only patches, in the order the gateway reported them.
        """
        ctx.logger.info("Extracting stories from %s", url)

        story_urls = self._pages.list_story_urls(url)
        patches: list[StoryPatch] = []

        for story_url in story_urls:
            patch = _patch_for(story_url, listing_url=url)
            if patch is None:
                ctx.logger.debug("Skipped an unusable story URL from %s: %s", url, story_url)
            else:
                patches.append(patch)

        if not patches:
            ctx.logger.warning("No stories found at %s", url)

        ctx.logger.info("Extracted %d story(ies) from %s", len(patches), url)
        return patches

    def _enrich(
        self,
        patches: list[StoryPatch],
        ctx: PluginContext,
        *,
        known_urls: frozenset[str],
        budget: _MetadataBudget,
    ) -> list[StoryPatch]:
        """Fill in metadata for the patches whose URL is not already known (EXT-D12).

        Fetches up to the remaining budget across all URLs in patches, sleeping
        ``request_delay_ms`` between requests, and only for URLs absent from *known_urls* — a
        story already listed is never re-fetched, so a large listing costs its full metadata
        exactly once, spread over as many scans as it takes. Reports progress 0–100 across the
        stories this call fetches; the caller is responsible for rescaling when the call is one
        slice of a larger run.

        Args:
            patches: The URL-only patches to enrich, in order.
            ctx: The plugin context (settings, logger, cancellation).
            known_urls: URLs this plugin already fetched metadata for; these are skipped.
            budget: The shared metadata budget for this scan, with remaining count and spacing.

        Returns:
            The patches, enriched where metadata was fetched, in input order. A returned
            patch is marked ``metadata_fetched=True`` iff the gateway returned a metadata
            dict for it (whether the dict was empty or not); a failed fetch deliberately
            leaves ``metadata_fetched`` at its ``False`` default so the core may offer that
            URL again later.
        """
        delay_ms = _delay_ms(ctx)

        # Identify which URLs need fetching
        todo = [p for p in patches if p.url not in known_urls]

        if not todo:
            ctx.logger.debug("Nothing to enrich: every story is already known")
            return patches

        # Cap the number of fetches
        to_fetch = todo[: budget.remaining]
        remaining = len(todo) - len(to_fetch)

        ctx.logger.info(
            "Enriching %d new story(ies) (cap %d, %dms apart)",
            len(todo),
            budget.remaining,
            delay_ms,
        )

        ok = 0
        parts = 0
        enriched_patches = dict((p.url, p) for p in patches)

        for index, patch in enumerate(to_fetch):
            ctx.check_cancelled()

            # Sleep between fetches (not before first)
            if budget.spaced:
                time.sleep(delay_ms / 1000)
            budget.spaced = True

            meta = self._pages.fetch_story_metadata(patch.url)
            budget.remaining -= 1
            if meta is None:
                ctx.logger.debug("No metadata for %s — keeping the URL-derived row", patch.url)
            else:
                part = _is_part(patch.url, patch.title or "", meta)
                enriched = replace(_apply_metadata(patch, meta), metadata_fetched=True)
                enriched_patches[patch.url] = enriched
                ok += 1
                if part:
                    parts += 1
                ctx.logger.debug(
                    'Enriched %s as a %s: title="%s", series="%s"',
                    patch.url,
                    "part" if part else "whole work",
                    enriched.title or "",
                    enriched.series or "-",
                )

            # One fetch is one step of this call's own work; the caller scales it if it is only a
            # slice of a larger run. Reported after the fetch, so 100% means "the last one is done".
            ctx.report(100.0 * (index + 1) / len(to_fetch))

        ctx.logger.info(
            "Enriched %d of %d new story(ies); %d left for the next scan",
            ok,
            len(todo),
            remaining,
        )

        if ok:
            ctx.logger.info(
                "Enrichment shape: %d part(s) of a larger work, %d whole work(s)",
                parts,
                ok - parts,
            )

        # Return patches in original order, with enriched versions where available
        patches_ordered = [enriched_patches[p.url] for p in patches]
        patches_ordered, applied, author = _propagate_author(patches_ordered)
        if applied:
            ctx.logger.info(
                'Applied the listing\'s one confirmed author to %d un-fetched row(s): author="%s"',
                applied,
                author,
            )
        elif ok:
            ctx.logger.debug(
                "Author propagation skipped: the %d fetched row(s) do not name one author",
                ok,
            )
        return patches_ordered

    def settings_schema(self) -> SettingsSchema:
        """Return the plugin's settings schema.

        Returns:
            The settings schema with extract_urls and metadata settings fields.
        """
        return self.manifest.settings_schema

    def claims_url(self, url: str) -> bool:
        """Return whether this plugin claims the given URL.

        Matches against the manifest's ``extract_url_patterns``, compiled once in ``__init__``.

        Args:
            url: The URL to check.

        Returns:
            True if the URL matches any pattern, False otherwise.
        """
        return any(pattern.search(url) for pattern in self._patterns)

    def extract_stories(self, url: str, ctx: PluginContext) -> list[StoryPatch]:
        """Return every story found at the given listing URL.

        The one-shot counterpart of :meth:`scan`. Metadata is fetched for stories not already
        stored, bounded by this call's own ``max_new_metadata_per_scan`` allowance.

        Raises: ListingError: propagated from the gateway — a scan that cannot read a watched page
            fails as a whole, so the results store is never written (standing rule, EXP-073).

        Args:
            url: The listing page URL.
            ctx: Runtime services for this call.

        Returns:
            Zero or more StoryPatch records, enriched where metadata was available.
        """
        patches = self._patches_for_listing(url, ctx)
        known_urls = decode_known_urls(ctx.ui_context.get(KNOWN_URLS_KEY))
        return self._enrich(
            patches, ctx, known_urls=known_urls, budget=_MetadataBudget(remaining=_cap(ctx))
        )

    def enrich_stories(self, urls: Sequence[str], ctx: PluginContext) -> list[StoryPatch]:
        """Fetch full metadata for stored story URLs without re-listing their page.

        Receives story URLs the core already stores, performs no listing request, applies
        this plugin's per-call metadata fetch budget and inter-request delay, and returns a
        ``StoryPatch`` only for URLs whose metadata was successfully fetched. Every returned
        patch carries ``metadata_fetched=True``. URLs whose metadata could not be fetched are
        omitted so the caller can offer them again later. Never returns a URL that was not in
        *urls*.

        Args:
            urls: Story URLs the core already stores, to fetch full metadata for.
            ctx: Runtime services for this call.

        Returns:
            Zero or more ``StoryPatch`` records, one per URL whose metadata was successfully
            fetched. Every returned patch carries ``metadata_fetched=True``. URLs whose
            metadata could not be fetched are omitted.
        """
        if not urls:
            ctx.logger.info("Metadata pass skipped: no URL was requested")
            return []

        ctx.logger.info("Metadata pass started: %d URL(s) requested, cap %d", len(urls), _cap(ctx))

        patches: list[StoryPatch] = []
        for url in urls:
            patch = _patch_for(url, listing_url="")
            if patch is None:
                ctx.logger.debug("Skipped an unusable story URL: %s", url)
            else:
                patches.append(patch)

        enriched = self._enrich(
            patches,
            ctx,
            known_urls=frozenset(),
            budget=_MetadataBudget(remaining=_cap(ctx)),
        )
        out = [p for p in enriched if p.metadata_fetched]
        ctx.logger.info("Metadata pass finished: %d of %d URL(s) enriched", len(out), len(urls))
        return out

    def scan(self, ctx: PluginContext) -> list[StoryPatch]:
        """Scan every watched URL and return all stories found, de-duplicated by URL.

        Listing is free, so every watched URL is listed. Metadata is the budgeted tier: the
        de-duplicated result is enriched in a single pass against one scan-wide allowance
        (``EXT-FR-10``), so a story two listings both contain is never fetched twice and ten
        watched URLs cost the configured cap once, not ten times.

        Raises: ListingError: propagated from the gateway — a scan that cannot read a watched page
            fails as a whole, so the results store is never written (standing rule, EXP-073).

        Args:
            ctx: Runtime services for this call.

        Returns:
            Zero or more StoryPatch records, de-duplicated by URL, enriched where metadata was
            available.
        """
        watched = ctx.settings.get("extract_urls") or []
        ctx.logger.info("Scan started: %d watched URL(s)", len(watched))

        if not watched:
            ctx.logger.info("Scan skipped: no watched URL is configured")
            return []

        seen_urls: set[str] = set()
        out: list[StoryPatch] = []
        for listing_url in watched:
            for patch in self._patches_for_listing(listing_url, ctx):
                if patch.url not in seen_urls:
                    seen_urls.add(patch.url)
                    out.append(patch)

        known_urls = decode_known_urls(ctx.ui_context.get(KNOWN_URLS_KEY))
        budget = _MetadataBudget(remaining=_cap(ctx))
        out = self._enrich(out, ctx, known_urls=known_urls, budget=budget)

        ctx.logger.info(
            "Scan finished: %d story(ies) from %d URL(s), %d metadata fetch(es) unspent",
            len(out),
            len(watched),
            budget.remaining,
        )
        return out
