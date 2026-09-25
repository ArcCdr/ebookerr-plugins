"""Lists story URLs and polls story metadata from an arbitrary page, via FanFicFare.

This module imports ``fanficfare`` in-process (``EXT-D7``, ``EXT-TR-1``) — the only
in-process use of a third-party converter library left in the core. Both callables are
injected so tests never touch the network.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fanficfare
from fanficfare import adapters
from fanficfare.configurable import Configuration
from fanficfare.geturls import get_urls_from_page

logger = logging.getLogger(__name__)

Lister = Callable[[str, Any, bool], dict[str, Any]]
MetadataFetcher = Callable[[str, Any], dict[str, Any] | None]

# one retry instead of FanFicFare's four; one-shot listing must fail in seconds
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
    """Lists story URLs and polls story metadata from an arbitrary page, via FanFicFare."""

    def __init__(
        self,
        personal_ini: Path,
        *,
        lister: Lister | None = None,
        metadata_fetcher: MetadataFetcher | None = None,
    ) -> None:
        """Store the config path and the injected callables.

        Args:
            personal_ini: Path to the FanFicFare Source's shared file, or this plugin's
                packaged default, layered over FanFicFare's own ``defaults.ini`` so the
                user's site logins and per-site options apply.
            lister: Callable ``(url, configuration, normalize) -> dict``; defaults to
                ``fanficfare.geturls.get_urls_from_page``. Injected in tests.
            metadata_fetcher: Callable ``(url, configuration) -> dict | None``; defaults to
                the adapter-driven metadata poll. Injected in tests.

            Configurations are cached per resolved section tuple, so a scan parses the
            ini files once.
        """
        self._personal_ini = personal_ini
        self._lister = lister or get_urls_from_page
        self._metadata_fetcher = metadata_fetcher or self._default_metadata_fetcher
        self._config_cache: dict[tuple[str, ...], Any] = {}

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
            result = self._metadata_fetcher(url, configuration)
            elapsed = time.monotonic() - start
            logger.debug("Fetched metadata for %s in %.1fs", url, elapsed)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch metadata for %s: %s", url, exc)
            return None

    def _cap_retries(self, config: Any, key: tuple[str, ...]) -> None:
        """Replace the fetcher's default four-retry backoff with ``LISTING_RETRIES`` (EXP-073).

        Reads ``Configuration.get_fetcher().retries`` (an ``urllib3`` ``Retry``) and swaps a copy in
        via ``Retry.new``; leaves a build without that attribute alone and logs at WARNING.

        Args:
            config: The FanFicFare Configuration object.
            key: The sections tuple used as the cache key.
        """
        get_fetcher = getattr(config, "get_fetcher", None)
        if get_fetcher is None:
            return
        fetcher = get_fetcher()
        retries = getattr(fetcher, "retries", None)
        if retries is None or not hasattr(retries, "new"):
            logger.warning(
                "Could not cap the FanFicFare retry budget for sections=%s: no retries attribute",
                ",".join(key),
            )
            return
        fetcher.retries = retries.new(total=LISTING_RETRIES, backoff_factor=1)
        logger.debug(
            "Capped the FanFicFare retry budget for sections=%s: total=%d",
            ",".join(key),
            LISTING_RETRIES,
        )

    def _configuration(self, url: str) -> Any:
        """Build (or reuse) a FanFicFare ``Configuration`` for *url*, layered over personal.ini.

        Configurations are cached on ``self`` keyed by the resolved section tuple: a scan that
        enriches many stories from one site would otherwise re-parse ``defaults.ini`` and
        ``personal.ini`` once per story. A failed parse is never cached, so a fixed
        ``personal.ini`` takes effect without restarting the process.

        Args:
            url: The story URL to configure for.

        Returns:
            A FanFicFare Configuration object, possibly shared with an earlier call.

        Raises:
            RuntimeError: ``personal.ini`` could not be parsed. Carries only the failing
                exception's type name — a parsing error quotes the offending line verbatim,
                which for ``personal.ini`` may be a site login credential, so its message never
                reaches this exception or any log call.
        """
        try:
            sections = adapters.getConfigSectionsFor(url)
        except Exception:  # noqa: BLE001
            sections = ["unknown"]

        key = tuple(sections)
        cached = self._config_cache.get(key)
        if cached is not None:
            logger.debug("Reusing the FanFicFare configuration for sections=%s", ",".join(key))
            return cached

        config = Configuration(sections, "EPUB", lightweight=True)

        # Find FanFicFare's defaults.ini
        fanficfare_dir = Path(fanficfare.__file__).parent
        defaults_ini = fanficfare_dir / "defaults.ini"

        try:
            config.read([str(defaults_ini), str(self._personal_ini)])
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not parse FanFicFare configuration: {type(exc).__name__}"
            ) from exc

        self._config_cache[key] = config
        self._cap_retries(config, key)
        logger.debug("Built a FanFicFare configuration for sections=%s", ",".join(key))
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
