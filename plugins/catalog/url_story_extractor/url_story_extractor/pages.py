"""Lists story URLs and polls story metadata from an arbitrary page, via FanFicFare.

This module uses FanFicFare in-process (``EXT-D7``, ``EXT-TR-1``) through the plugin's
FanFicFare support module (``LIB-D26``), configured from the plugin's own settings and the
stored sign-in of the page's site. Both callables are injected so tests never touch the network.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from ebookerr_sdk.spi import SiteCredential
from fanficfare import adapters
from fanficfare.geturls import get_urls_from_page

from url_story_extractor.fff_support import (
    apply_sign_in,
    build_configuration,
    captured_stdout,
    config_sections,
    packaged_base_ini,
    quiet_fanficfare_logging,
    settings_options,
)

logger = logging.getLogger(__name__)

Lister = Callable[[str, Any, bool], dict[str, Any]]
MetadataFetcher = Callable[[str, Any], dict[str, Any] | None]

# one retry instead of FanFicFare's four, through its own max_request_retries option
# (EXP-073); one-shot listing must fail in seconds
LISTING_RETRIES = 1


class ListingError(RuntimeError):
    """A listing page could not be read.

    Raised when a network failure, an unreadable page or a malformed response
    prevents listing stories at a URL. Raised instead of returning an empty list
    so a scan that cannot reach its source fails as a whole and the catalog store
    is never written with a false "nothing here" snapshot.

    Its message names the listing and the cause and is shown to the user verbatim
    (``user_facing``).
    """

    user_facing = True


class FanFicFarePagesGateway:
    """Lists story URLs and polls story metadata from an arbitrary page, via FanFicFare.

    FanFicFare is configured from the plugin's own settings (``is_adult`` and the "Advanced
    FanFicFare options") and the stored sign-in of the page's site, nothing else.
    """

    def __init__(
        self,
        settings: Mapping[str, Any],
        *,
        credentials: Callable[[str], SiteCredential | None] | None = None,
        lister: Lister | None = None,
        metadata_fetcher: MetadataFetcher | None = None,
    ) -> None:
        """Store the plugin's settings, the sign-in lookup and the injected callables.

        Configurations are cached per resolved section tuple and host, so a scan builds each
        site's configuration once. Quiets FanFicFare's logging (``LIB-D26``).

        Args:
            settings: The plugin's resolved settings (``ctx.settings``); ``is_adult`` and
                ``extra_options`` are layered over FanFicFare's own ``defaults.ini``.
            credentials: Looks up the stored sign-in of a page's site (``ctx.credentials``);
                ``None`` applies no sign-in.
            lister: Callable ``(url, configuration, normalize) -> dict``; defaults to
                ``fanficfare.geturls.get_urls_from_page``. Injected in tests.
            metadata_fetcher: Callable ``(url, configuration) -> dict | None``; defaults to
                the adapter-driven metadata poll. Injected in tests.
        """
        self._settings = settings
        self._credentials = credentials
        self._lister = lister or get_urls_from_page
        self._metadata_fetcher = metadata_fetcher or self._default_metadata_fetcher
        self._config_cache: dict[tuple[tuple[str, ...], str], Any] = {}
        quiet_fanficfare_logging()

    def list_story_urls(self, url: str) -> list[str]:
        """Return every story URL found at *url*, or ``[]`` if the page was read but empty.

        Raises: ListingError: the page could not be listed; the cause is chained.
            An empty ``urllist`` from a page that *was* read still returns ``[]`` —
            that is a genuinely empty page.
        Requests each story's canonical URL from FanFicFare rather than an arbitrary chapter link.

        Args:
            url: The listing page URL.

        Returns:
            The story URLs, de-duplicated, in the order FanFicFare reported them.
        """
        logger.info("Listing stories at %s", url)
        start = time.monotonic()
        try:
            configuration = self._configuration(url)
            # normalize=True asks FanFicFare for each story's own canonical URL instead of the
            # longest raw href it saw. That is the URL the adapter reports as `storyUrl`, so a
            # catalog row and the book it later becomes share one identity — and it drops page
            # fragments and arbitrary chapter links that would otherwise be stored as the story.
            with captured_stdout():
                result = self._lister(url, configuration, True)

            urls = result.get("urllist", [])
            if not isinstance(urls, list):
                urls = []

            # Filter to strings only and deduplicate while preserving order
            seen = set()
            deduped = []
            for item in urls:
                if isinstance(item, str) and item not in seen:
                    seen.add(item)
                    deduped.append(item)

            elapsed = time.monotonic() - start
            logger.info("Listed %d story URL(s) at %s in %.1fs", len(deduped), url, elapsed)
            return deduped
        except ListingError:  # noqa: BLE001
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Listing failed at %s: %s: %s", url, type(exc).__name__, exc)
            raise ListingError(f"could not list stories at {url}: {exc}") from exc

    def fetch_story_metadata(self, url: str) -> dict[str, Any] | None:
        """Return the raw metadata dict for one story URL, or ``None`` on any failure.

        Args:
            url: The story URL.

        Returns:
            FanFicFare's own metadata mapping, untouched and uncoerced, or ``None``.
        """
        logger.debug("Fetching metadata for %s", url)
        start = time.monotonic()
        try:
            configuration = self._configuration(url)
            with captured_stdout():
                result = self._metadata_fetcher(url, configuration)
            elapsed = time.monotonic() - start
            logger.debug("Fetched metadata for %s in %.1fs", url, elapsed)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch metadata for %s: %s", url, exc)
            return None

    def _configuration(self, url: str) -> Any:
        """Build (or reuse) a FanFicFare ``Configuration`` for *url*, from settings and sign-in.

        FanFicFare's own ``defaults.ini`` comes first, then this plugin's packaged base options,
        its settings, its "Advanced FanFicFare options" and the retry cap; the site's stored
        sign-in, when the user has one, goes on top (``C36``).

        Configurations are cached on ``self`` keyed by the resolved section tuple and the host: a
        scan that enriches many stories from one site would otherwise rebuild the configuration
        once per story, and a sign-in is never applied to a page of another site. A failed parse
        is never cached, so corrected options take effect without restarting the process.
        FanFicFare's own ``max_request_retries`` option caps the retry budget at
        ``LISTING_RETRIES`` (``EXP-073``).

        Args:
            url: The story or listing URL to configure for.

        Returns:
            A FanFicFare Configuration object, possibly shared with an earlier call for the same
            site.

        Raises:
            ConfigurationError: The advanced options could not be parsed (a ``RuntimeError``).
                Carries only the failing exception's type name — a parsing error quotes the
                offending line verbatim, which may be a site login credential, so its message
                never reaches this exception or any log call.
        """
        sections = config_sections(url, unknown_site_ok=True)
        host = (urlsplit(url).hostname or "").lower()
        key = (tuple(sections), host)
        cached = self._config_cache.get(key)
        if cached is not None:
            logger.debug("Reusing the FanFicFare configuration for sections=%s", ",".join(key[0]))
            return cached

        config = build_configuration(
            sections,
            fileform="EPUB",
            base_ini=packaged_base_ini(),
            options=settings_options(self._settings),
            extra_options=str(self._settings.get("extra_options") or ""),
            overrides={"max_request_retries": str(LISTING_RETRIES)},
            lightweight=True,
        )
        apply_sign_in(
            config, self._credentials(url) if self._credentials is not None else None, host
        )
        self._config_cache[key] = config
        logger.debug("Built a FanFicFare configuration for sections=%s", ",".join(key[0]))
        return config

    def _default_metadata_fetcher(self, url: str, configuration: Any) -> dict[str, Any] | None:
        """Default metadata fetcher using adapter-driven poll.

        Args:
            url: The story URL.
            configuration: FanFicFare Configuration object.

        Returns:
            Metadata dict or None.
        """
        try:
            adapter = adapters.getAdapter(configuration, url)
            metadata = adapter.getStoryMetadataOnly()
            result = metadata.getAllMetadata()
            return result if isinstance(result, dict) else None
        except Exception:  # noqa: BLE001
            return None
