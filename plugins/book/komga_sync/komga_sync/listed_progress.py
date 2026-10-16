"""The books the last minute check listed with reading progress, kept in the plugin's own state.

Komga marks a book unread by deleting its read progress, so the book leaves the newest-first
progress listing ``KomgaService.read_state_changes`` reads and leaves no stamp behind (EXP-344).
Remembering which books the last check listed with progress lets the next check notice one that
vanished. The memory lives in ``ctx.state_*`` (SPI 2.30, ``PMG-D32``) under keys
``listed_progress:<library_id>``, so it survives restarts.
"""

from __future__ import annotations

from ebookerr_sdk.spi import PluginContext

_KEY_PREFIX = "listed_progress:"


class ListedProgress:
    """Per-library memory of the book ids the last minute check listed with progress."""

    def __init__(self, ctx: PluginContext) -> None:
        """Keep the plugin context whose state holds the memory.

        Args:
            ctx: The plugin context (``state_get`` / ``state_set``).
        """
        self._ctx = ctx

    def recall(self, library_id: str) -> list[str]:
        """Return the ids the last check listed with progress, or ``[]`` when none is remembered.

        Args:
            library_id: The Komga library.

        Returns:
            The remembered ids; a missing or malformed value reads as ``[]``.
        """
        value = self._ctx.state_get(_KEY_PREFIX + library_id)
        if not isinstance(value, list):
            return []
        return [book_id for book_id in value if isinstance(book_id, str)]

    def remember(self, library_id: str, book_ids: list[str]) -> None:
        """Record *book_ids* as the books this check listed with progress.

        Args:
            library_id: The Komga library.
            book_ids: The listed ids that carry reading progress, at most one listing page.
        """
        self._ctx.state_set(_KEY_PREFIX + library_id, list(book_ids))
