"""The Patreon memberships catalog plugin, served by ``ebookerr_sdk.host``.

``PatreonStoriesPlugin`` is the SPI ``CatalogPlugin``: its ``scan`` asks the core for the stored
patreon.com sign-in with ``ctx.credentials``, runs ``catalog.scan`` over every membership the
account holds and answers the story dicts that returns as ``StoryPatch`` objects. The plugin's
context carries the progress reports and the circuit guard the SDK host turns into wire frames; a
scan failure is answered by its message alone (``CatalogScanError``), a host that cannot be reached
included; a host whose breaker is already open answers an empty scan with a warning instead; and
the catalog's own log entries reach the host's log through the plugin's logger, texts and levels
unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.spi import PluginContext, SettingsSchema, StoryPatch
from ebookerr_sdk.spi.manifest import package_manifest

from patreon_stories import catalog


class CatalogScanError(RuntimeError):
    """A scan failure whose message is written for the reader; the SDK host reports it verbatim."""

    user_facing = True


_STORY_FIELDS: tuple[str, ...] = (
    "story_id",
    "title",
    "author",
    "author_url",
    "series",
    "series_url",
    "description",
    "category",
    "tags",
    "status",
    "num_chapters",
    "num_words",
    "cover_image",
    "site",
    "date_published",
    "date_updated",
    "rating",
    "format",
    "custom",
    "metadata_fetched",
)
"""Every ``StoryPatch`` field besides ``url``."""

_LOG_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
"""The levels of the catalog's ``{"level", "message"}`` log entries."""


def _story_patch(story: Mapping[str, Any]) -> StoryPatch:
    """Build the SPI ``StoryPatch`` from one story dict this catalog's mapping returns.

    Absent and ``None`` fields stay unset and a date the SDK cannot parse is left unset; the core
    validates every field again when it decodes the patch.

    Args:
        story: A story dict with at least ``url``.

    Returns:
        The story as a ``StoryPatch``.
    """
    fields: dict[str, Any] = {
        name: story[name] for name in _STORY_FIELDS if story.get(name) is not None
    }
    for name in ("date_published", "date_updated"):
        if name in fields:
            parsed = parse_datetime(fields.pop(name))
            if parsed is not None:
                fields[name] = parsed
    return StoryPatch(url=str(story["url"]), **fields)


def _emit_logs(logger: logging.Logger, entries: Sequence[Mapping[str, Any]]) -> None:
    """Log the catalog's ``{"level", "message"}`` entries through *logger*, in order."""
    for entry in entries:
        logger.log(
            _LOG_LEVELS.get(str(entry.get("level")), logging.INFO), "%s", entry.get("message", "")
        )


class PatreonStoriesPlugin:
    """The Patreon memberships catalog: the story files of every membership the account holds."""

    manifest = package_manifest(__file__)

    def settings_schema(self) -> SettingsSchema:
        """Return the settings the manifest declares."""
        return self.manifest.settings_schema

    def scan(self, ctx: PluginContext) -> list[StoryPatch]:
        """Scan every Patreon membership as ``StoryPatch``es, signed in with the stored cookie.

        Asks the core for the stored patreon.com sign-in (``ctx.credentials``) and hands it, with
        the plugin's settings and the context, to ``catalog.scan``.

        Args:
            ctx: Runtime services for this call; its credentials hold the patreon.com sign-in and
                its settings the look-back window.

        Returns:
            One ``StoryPatch`` per unique story file the memberships' recent posts carry, or an
            empty list when the host's circuit breaker is open (a warning says so).

        Raises:
            CatalogScanError: The scan failed, the failure's own message; this includes a host
                that could not be reached while its breaker is still closed, since an empty
                result means the source really has nothing.
        """
        try:
            stories, logs = catalog.scan(
                ctx.credentials(catalog.SIGN_IN_URL), dict(ctx.settings), ctx
            )
        except catalog.CatalogHostUnreachable as exc:
            # Only a host the breaker already holds is a quiet empty scan; any other failure to
            # reach it fails the scan, since an empty result means the source has nothing.
            if not ctx.circuit.is_open(catalog.CIRCUIT_KEY):
                detail = f": {exc.__cause__}" if exc.__cause__ is not None else ""
                raise CatalogScanError(f"{catalog.CIRCUIT_LABEL} is not reachable{detail}") from exc
            ctx.logger.warning(
                "%s is not reachable, so this scan made no request."
                " It will be retried automatically.",
                catalog.CIRCUIT_LABEL,
            )
            return []
        except Exception as exc:  # noqa: BLE001 — any other failure is answered by its message
            raise CatalogScanError(str(exc)) from exc
        _emit_logs(ctx.logger, logs)
        return [_story_patch(story) for story in stories]
