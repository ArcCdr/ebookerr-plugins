"""FanFicFareSourcePlugin — catch-all Source plugin wrapping the FanFicFare pull engine.

**The catch-all floor**: ``claims(url)`` is ``url.startswith(("http://", "https://"))``, so
this plugin claims any HTTP(S) URL nothing more specific claims first — a third-party (or
first-party) Source registers with a lower ``priority`` number than this plugin's ``1000``
to be preferred for the URLs it recognises. The two bundled ``EpubDownloadSourcePlugin``/
``PdfDownloadSourcePlugin`` (``priority=100``) are first-party examples of exactly this:
they claim ``.epub``/``.pdf`` URLs (or a matching ``media_format`` hint) ahead of this
floor and skip FanFicFare's dependency chain entirely.

This is a thin wrapper: every call is forwarded to the injected :class:`FanFicFarePull`
engine, which does the actual work (meta poll, no-new-content skip, download, verify,
derive) and returns one complete ``BookPatch``. The core orchestrator applies that patch
through the generic create/update path — this plugin persists nothing itself. The core owns
the staging-dir lifecycle, post-process, publish, chapter-count recompute (``DEC-31``),
cover, baseline and notification, and provides the ``work_dir``.

``check_for_update()`` delegates to the engine likewise; the Auto-Pull scan calls it
directly when this Source is selected for a URL (``update_check=True`` in the manifest).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    PluginContext,
    SettingsSchema,
    SourcePullError,
    UpdateCheck,
)
from ebookerr_sdk.spi.manifest import package_manifest

if TYPE_CHECKING:
    from fanficfare_source.pull import FanFicFarePull

logger = logging.getLogger(__name__)

__all__ = ["FanFicFareSourcePlugin", "SourcePullError", "remove_leftover_personal_ini"]


def remove_leftover_personal_ini() -> None:
    """Delete the ``personal.ini`` earlier releases kept in this plugin's data folder (``D54``).

    FanFicFare's options are now this plugin's settings and site sign-ins live in Settings →
    Credentials, so nothing reads the file. It is removed and logged once; when it is absent
    nothing happens.
    """
    data_dir = os.environ.get("EBOOKERR_PLUGIN_DATA_DIR")
    if not data_dir:
        return
    path = Path(data_dir) / "personal.ini"
    if not path.is_file():
        return
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path, exc)
        return
    logger.info("personal.ini is no longer used; removed %s", path)


class FanFicFareSourcePlugin:
    """Catch-all Source plugin for FanFicFare downloads."""

    manifest = package_manifest(__file__)

    def __init__(self, *, pull: FanFicFarePull | None = None) -> None:
        """Store the injected pull engine.

        Args:
            pull: The DB-free :class:`FanFicFarePull` engine this plugin delegates to.
                ``None`` is accepted for manifest/claims-only construction (as the container
                always injects one today); calling :meth:`pull` or :meth:`check_for_update`
                with no engine injected raises.
        """
        self._pull = pull

    def _engine(self, ctx: PluginContext | None) -> FanFicFarePull:
        """Return the pull engine: injected at construction, or built on first use.

        A built engine runs FanFicFare on the call's own settings, stored sign-ins and circuit
        breakers; with no context it runs on FanFicFare's defaults, with no sign-in and no breaker.

        Args:
            ctx: Runtime context (out of process): its settings, sign-ins and circuit breakers.

        Returns:
            The :class:`FanFicFarePull` engine.
        """
        remove_leftover_personal_ini()
        if self._pull is not None:
            return self._pull
        from fanficfare_source.library import FanFicFareLibraryGateway
        from fanficfare_source.pull import FanFicFarePull

        if ctx is None:
            return FanFicFarePull(FanFicFareLibraryGateway({}))
        return FanFicFarePull(
            FanFicFareLibraryGateway(ctx.settings, credentials=ctx.credentials, circuit=ctx.circuit)
        )

    def settings_schema(self) -> SettingsSchema:
        """Return the settings the manifest declares (C36)."""
        return self.manifest.settings_schema

    def claims(self, url: str) -> bool:
        """Claim any HTTP(S) URL — the catch-all floor beneath every more specific Source."""
        return url.startswith(("http://", "https://"))

    def check_for_update(
        self, url: str, *, prior: BookView | None, ctx: PluginContext
    ) -> UpdateCheck:
        """Poll for updates via FanFicFare.

        Delegates to the pull engine. Used by the Auto-Pull scan to determine whether a
        book needs re-download before submitting the pull task.

        Args:
            url: The book's source URL.
            prior: The book's current snapshot, or ``None`` when unknown.
            ctx: The call's plugin context, passed to the pull engine.
        """
        return self._engine(ctx).check_for_update(url, prior=prior, ctx=ctx)

    def pull(
        self,
        url: str,
        work_dir: Path,
        prior: BookView | None,
        ctx: PluginContext,
    ) -> BookPatch:
        """Run the FanFicFare pull into the core-provided ``work_dir``.

        Returns a complete ``BookPatch`` (``upsert=False`` for a touched-but-unchanged pull,
        ``upsert=True`` after a fresh download). Raises ``SourcePullError`` when the
        download/verify fails — the core maps it to a non-fatal per-book error.
        """
        return self._engine(ctx).pull(url, work_dir, prior, ctx)
