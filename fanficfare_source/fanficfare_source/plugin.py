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
through the generic create/update path — this plugin persists nothing itself. It owns the
staging-dir lifecycle, post-process, publish, chapter-count recompute (``DEC-31``), cover,
baseline and notification, and provides the ``work_dir``.

``check_for_update()`` delegates to the engine likewise; the Auto-Pull scan calls it
directly when this Source is selected for a URL (``update_check=True`` in the manifest).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    PluginContext,
    PluginManifest,
    PluginType,
    SettingsSchema,
    SourcePullError,
    UpdateCheck,
)

if TYPE_CHECKING:
    from fanficfare_source.pull import FanFicFarePull

logger = logging.getLogger(__name__)

__all__ = ["FanFicFareSourcePlugin", "SourcePullError", "personal_ini_path"]


def personal_ini_path() -> Path:
    """Return this install's FanFicFare configuration file, seeding it on first use (D37).

    The file lives in the plugin's data folder (``EBOOKERR_PLUGIN_DATA_DIR``) and holds the user's site
    logins; it is copied from the packaged default when absent. A leading UTF-8 byte-order mark is
    removed, because FanFicFare's ini reader fails on one (TXE-D1).

    Returns:
        The path of the configuration file to pass to FanFicFare.
    """
    data_dir = Path(os.environ["EBOOKERR_PLUGIN_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "personal.ini"
    if not target.exists():
        shutil.copyfile(Path(__file__).with_name("personal.ini"), target)
        logger.info("Seeded the FanFicFare configuration at %s", target)
    raw = target.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        target.write_bytes(raw[3:])
        logger.info("Removed a UTF-8 byte-order mark from %s", target.name)
    return target

_MANIFEST = PluginManifest(
    id="fanficfare_source",
    name="FanFicFare",
    description=(
        "Downloads stories from any site FanFicFare supports. This is the fallback source: "
        "it handles every web address no more specific source claims first."
    ),
    version="1.1.0",
    plugin_type=PluginType.SOURCE,
    settings_schema=SettingsSchema(),
    headless=True,
    events=(),
    priority=1000,
    url_patterns=(r"^https?://",),
    catch_all=True,
    update_check=True,
    default_enabled=True,
    run_timeout_s=3600,
    icon="auto_stories",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/fanficfare_source",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/fanficfare_source",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    requirements=("fanficfare>=4.58.1",),
)


class FanFicFareSourcePlugin:
    """Catch-all Source plugin for FanFicFare downloads."""

    manifest = _MANIFEST

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

        Args:
            ctx: Runtime context (out of process) with circuit breaker access.

        Returns:
            The :class:`FanFicFarePull` engine.
        """
        if self._pull is not None:
            return self._pull
        from fanficfare_source.cli import FanFicFareCliGateway
        from fanficfare_source.pull import FanFicFarePull

        circuit = getattr(ctx, "circuit", None) if ctx else None
        return FanFicFarePull(FanFicFareCliGateway(personal_ini_path(), circuit=circuit))

    def settings_schema(self) -> SettingsSchema:
        """Return the (empty) settings schema — this plugin has no user-configurable options."""
        return SettingsSchema()

    def claims(self, url: str) -> bool:
        """Claim any HTTP(S) URL — the catch-all floor beneath every more specific Source."""
        return url.startswith(("http://", "https://"))

    def check_for_update(
        self,
        url: str,
        *,
        prior: BookView | None = None,
        cancel_event: threading.Event | None = None,
        ctx: PluginContext | None = None,
    ) -> UpdateCheck:
        """Poll for updates via FanFicFare.

        Delegates to the pull engine. Used by the Auto-Pull scan to determine whether a
        book needs re-download before submitting the pull task.

        Args:
            url: The book's source URL.
            prior: The book's current snapshot, or ``None`` when unknown.
            cancel_event: Set when the user cancels the scan; propagated to the engine.
            ctx: Runtime services for the check (plugin state, auth headers), when the core
                supplies them (SPI 2.30).
        """
        return self._engine(ctx).check_for_update(
            url, prior=prior, ctx=ctx, cancel_event=cancel_event
        )

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
