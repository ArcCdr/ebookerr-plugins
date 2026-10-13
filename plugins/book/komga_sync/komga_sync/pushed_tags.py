"""The tag set this plugin last pushed to each Komga book, kept in the plugin's own state.

Komga stores a book's tags as one flat list that this plugin overwrites on every push
(``_compose_tags``), while the tag import reads Komga *before* this sync's push. Without a
memory, a category the plugin pushed last time looks like a tag the user added in Komga and is
imported into ``tags`` (EXP-276). The memory lives in ``ctx.state_*`` (SPI 2.30, ``PMG-D32``)
under keys ``pushed_tags:<book_id>``, so it survives restarts.
"""

from __future__ import annotations

from ebookerr_sdk.spi import PluginContext

_KEY_PREFIX = "pushed_tags:"


class PushedTags:
    """Per-book memory of the tags this plugin last pushed to Komga (EXP-276)."""

    def __init__(self, ctx: PluginContext) -> None:
        """Keep the plugin context whose state holds the memory.

        Args:
            ctx: The plugin context (``state_get`` / ``state_set``).
        """
        self._ctx = ctx

    def recall(self, book_id: str) -> list[str]:
        """Return the tags last pushed for *book_id*, or ``[]`` when none is remembered.

        Args:
            book_id: The ebookerr book id.

        Returns:
            The remembered tag list; a missing or malformed value reads as ``[]``.
        """
        value = self._ctx.state_get(_KEY_PREFIX + book_id)
        if not isinstance(value, list):
            return []
        return [t for t in value if isinstance(t, str)]

    def remember(self, book_id: str, tags: list[str]) -> None:
        """Record *tags* as the set Komga now holds for *book_id*.

        Args:
            book_id: The ebookerr book id.
            tags: The tag list Komga holds after this sync's push.
        """
        self._ctx.state_set(_KEY_PREFIX + book_id, list(tags))
