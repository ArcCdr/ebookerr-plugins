"""DocxDownloadSourcePlugin — direct .docx URL downloads converted to EPUB.

Takes the title and author from the converted document. Handles direct DOCX downloads from URLs
ending in .docx. Applies site authentication headers (cookies, basic auth, etc.) resolved via the
SPI ``PluginContext.auth_headers``, converts the DOCX to EPUB via convert_docx_to_epub, reads
EPUB metadata, and returns a BookPatch suitable for the core orchestrator.

Unlike FanFicFare (priority=1000 catch-all), this plugin claims .docx URLs directly
(priority=100) and skips FanFicFare's dependency chain for pure DOCX downloads.
"""

from __future__ import annotations

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

from docx_download_source.docx_to_epub import convert_docx_to_epub

__all__ = ["DocxDownloadSourcePlugin"]

_MANIFEST = PluginManifest(
    id="docx_download_source",
    name="DOCX download",
    version="1.1.0",
    plugin_type=PluginType.SOURCE,
    settings_schema=SettingsSchema(),
    priority=100,
    events=(),
    url_patterns=(r"(?i)^[^:/?#]+://[^/?#]*/[^?#]*\.docx(?:[?#]|$)",),
    description="Downloads a DOCX and converts it to EPUB in the library.",
    update_check=True,
    default_enabled=True,
    run_timeout_s=900,
    icon="description",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/docx_download_source",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/docx_download_source",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    requirements=("python-docx>=1.1",),
    formats=("docx",),
)


class DocxDownloadSourcePlugin:
    """Direct Source plugin for DOCX downloads with site authentication."""

    manifest = _MANIFEST

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        """Store the HTTP session — a fresh ``requests.Session`` when none is given (the plugin process serves one call) — and the request timeout."""
        self._session = session if session is not None else requests.Session()
        self._timeout_s = timeout_s

    def settings_schema(self) -> SettingsSchema:
        """Return the (empty) settings schema — this plugin has no user-configurable options."""
        return SettingsSchema()

    def claims(self, url: str) -> bool:
        """Claim absolute URLs whose path ends in ``.docx`` (any case; query and fragment ignored) — the same rule as the manifest's ``url_patterns``, which is what the core applies out of process."""
        path = urlsplit(url).path.lower()
        return path.endswith(".docx")

    def claims_format(self, media_format: str) -> bool:
        """Claim the 'docx' media format."""
        return media_format == "docx"

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
        """Download DOCX, convert to EPUB, stage file, and return BookPatch.

        Raises:
            SourcePullError: On download failure, invalid DOCX, or conversion failure.
            ContentTypeMismatchError: When the server's Content-Type contradicts the claimed format.
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
            source_suffix=".docx",
            fmt="docx",
            convert=convert_docx_to_epub,
            log_label="DOCX",
        )
