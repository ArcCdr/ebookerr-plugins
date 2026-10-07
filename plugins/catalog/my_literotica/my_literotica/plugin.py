"""The My Literotica catalog plugin, served by ``ebookerr_sdk.host``.

``MyLiteroticaPlugin`` is the SPI ``CatalogPlugin``: its ``scan`` asks the core for the stored
literotica.com sign-in with ``ctx.credentials``, runs ``catalog.scan`` over the activity wall and
answers the story dicts that returns as ``StoryPatch`` objects. The plugin's context carries the
progress reports and the circuit guard the SDK host turns into wire frames; a scan failure is
answered by its message alone (``CatalogScanError``); a host whose breaker is open answers an
empty scan with a warning; and the catalog's own log entries reach the host's log through the
plugin's logger, texts and levels unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.spi import PluginContext, SettingsSchema, StoryPatch
from ebookerr_sdk.spi.manifest import package_manifest

from my_literotica import catalog


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


class MyLiteroticaPlugin:
    """The My Literotica catalog: the story publications of the authors the user follows."""

    manifest = package_manifest(__file__)

    def settings_schema(self) -> SettingsSchema:
        """Return the settings the manifest declares."""
        return self.manifest.settings_schema

    def scan(self, ctx: PluginContext) -> list[StoryPatch]:
        """Scan the activity wall as ``StoryPatch``es, signed in with the stored sign-in.

        Asks the core for the stored literotica.com sign-in (``ctx.credentials``) and hands it,
        with the context, to ``catalog.scan``.

        Args:
            ctx: Runtime services for this call; its credentials hold the literotica.com sign-in.

        Returns:
            One ``StoryPatch`` per unique story publication the wall holds, or an empty list
            when the host's circuit breaker is open (a warning says so).

        Raises:
            CatalogScanError: The scan failed; its message is the failure's own message.
        """
        try:
            stories, logs = catalog.scan(ctx.credentials(catalog.SIGN_IN_URL), ctx)
        except catalog.CatalogHostUnreachable:
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
