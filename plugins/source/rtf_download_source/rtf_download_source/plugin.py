"""RtfDownloadSourcePlugin — direct .rtf URL downloads converted to EPUB.

Handles direct RTF downloads from URLs ending in .rtf. Applies site authentication
headers (cookies, basic auth, etc.) resolved via the SPI ``PluginContext.auth_headers``,
converts the RTF to EPUB via convert_rtf_to_epub, takes the title from the document's
first line, and returns a BookPatch suitable for the core orchestrator.

Unlike FanFicFare (priority=1000 catch-all), this plugin claims .rtf URLs directly
(priority=100) and skips FanFicFare's dependency chain for pure RTF downloads.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

import requests
from ebookerr_sdk.download.check import check_download_update
from ebookerr_sdk.download.document import download_convert_stage
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    PluginContext,
    PluginManifest,
    PluginType,
    SettingsSchema,
    UpdateCheck,
)

from rtf_download_source.rtf_to_epub import convert_rtf_to_epub

__all__ = ["RtfDownloadSourcePlugin"]

logger = logging.getLogger(__name__)

_MANIFEST = PluginManifest(
    id="rtf_download_source",
    name="RTF download",
    version="1.1.0",
    plugin_type=PluginType.SOURCE,
    settings_schema=SettingsSchema(),
    priority=100,
    events=(),
    url_patterns=(r"(?i)^[^:/?#]+://[^/?#]*/[^?#]*\.rtf(?:[?#]|$)",),
    description="Downloads an RTF and converts it to EPUB in the library.",
    update_check=True,
    default_enabled=True,
    run_timeout_s=900,
    icon="text_snippet",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/rtf_download_source",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/rtf_download_source",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    requirements=("striprtf>=0.0.26", "charset-normalizer>=3.3"),
    formats=("rtf",),
)


class RtfDownloadSourcePlugin:
    """Direct Source plugin for RTF downloads with site authentication."""

    manifest = _MANIFEST

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        """Store the HTTP session and the request timeout.

        A fresh ``requests.Session`` is created when none is given, since the plugin
        process serves exactly one call.
        """
        self._session = session if session is not None else requests.Session()
        self._timeout_s = timeout_s

    def settings_schema(self) -> SettingsSchema:
        """Return the plugin's settings schema (none configurable)."""
        return SettingsSchema()

    def claims(self, url: str) -> bool:
        """Claim absolute URLs whose path ends in ``.rtf``.

        Case-insensitive; query and fragment are ignored — the same rule as the
        manifest's ``url_patterns``, which is what the core applies out of process.
        """
        path = urlsplit(url).path.lower()
        return path.endswith(".rtf")

    def claims_format(self, media_format: str) -> bool:
        """Claim the 'rtf' media format."""
        return media_format == "rtf"

    def check_for_update(
        self,
        url: str,
        *,
        prior: BookView | None = None,
        cancel_event: threading.Event | None = None,
    ) -> UpdateCheck:
        """Check for update via HEAD request and validator comparison.

        The HEAD request carries no site credentials.
        """
        return check_download_update(
            url,
            session=self._session,
            auth_headers={},
            prior=prior,
            namespace=self.manifest.id,
            cancel_event=cancel_event,
        )

    def pull(
        self,
        url: str,
        work_dir: Path,
        prior: BookView | None,
        ctx: PluginContext,
    ) -> BookPatch:
        """Download RTF, convert to EPUB, stage file, and return BookPatch.

        Raises SourcePullError on download failure, invalid RTF, or conversion failure.
        """
        return download_convert_stage(
            url,
            work_dir,
            prior,
            ctx,
            session=self._session,
            auth_headers=ctx.auth_headers(url),
            timeout_s=self._timeout_s,
            namespace=self.manifest.id,
            source_suffix=".rtf",
            fmt="rtf",
            convert=convert_rtf_to_epub,
            log_label="RTF",
        )
