"""PdfDownloadSourcePlugin — direct .pdf URL downloads converted to EPUB.

Handles direct PDF downloads from URLs ending in .pdf. A thin Source wrapper: ``pull()``
delegates the whole download -> hash -> convert -> stage flow to the shared
:func:`~ebookerr_sdk.download.document.download_convert_stage` helper (also used by the
bundled DOCX/RTF/TXT download Sources), passing
:func:`~pdf_download_source.pdf_to_epub.convert_pdf_to_epub` as the conversion function — so the
hash-skip, filename/title precedence, and staging logic are written once and shared, not
reimplemented per format. Site authentication headers (cookies, basic auth, etc.) come
from the SPI via ``PluginContext.auth_headers``.

Unlike FanFicFare (priority=1000 catch-all), this plugin claims .pdf URLs directly
(priority=100) and skips FanFicFare's dependency chain for pure PDF downloads. Its
returned patch carries real ``fields`` (unlike FanFicFare's self-persisted, empty-
``fields`` patch), so it flows through the ordinary ``apply_book_patch``
create-from-fields path, including the unique-filename finalization step.

``pymupdf`` is this plugin's declared requirement (``requirements`` in its manifest);
the image installs it until plugins install their own requirements.
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

from pdf_download_source.pdf_to_epub import convert_pdf_to_epub

__all__ = ["PdfDownloadSourcePlugin"]

_MANIFEST = PluginManifest(
    id="pdf_download_source",
    name="PDF download",
    version="1.1.0",
    plugin_type=PluginType.SOURCE,
    settings_schema=SettingsSchema(),
    priority=100,
    events=(),
    url_patterns=(r"(?i)^[^:/?#]+://[^/?#]*/[^?#]*\.pdf(?:[?#]|$)",),
    description="Downloads a PDF and converts it to EPUB in the library.",
    update_check=True,
    default_enabled=True,
    run_timeout_s=900,
    icon="picture_as_pdf",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/pdf_download_source",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/pdf_download_source",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    requirements=("pymupdf>=1.24",),
    formats=("pdf",),
)


class PdfDownloadSourcePlugin:
    """Direct Source plugin for PDF downloads with site authentication."""

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
        """Return the (empty) settings schema — this plugin has no user-configurable options."""
        return SettingsSchema()

    def claims(self, url: str) -> bool:
        """Claim absolute URLs whose path ends in ``.pdf``.

        Case-insensitive; query and fragment are ignored — the same rule as the
        manifest's ``url_patterns``, which is what the core applies out of process.
        """
        path = urlsplit(url).path.lower()
        return path.endswith(".pdf")

    def claims_format(self, media_format: str) -> bool:
        """Claim the 'pdf' media format."""
        return media_format == "pdf"

    def check_for_update(
        self,
        url: str,
        *,
        prior: BookView | None = None,
        cancel_event: threading.Event | None = None,
    ) -> UpdateCheck:
        """Check for update via HEAD request and validator comparison.

        Note: The HEAD request carries no site credentials.
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
        """Download PDF, convert to EPUB, stage file, and return BookPatch.

        The raw PDF bytes are hashed **before** conversion — the source identity for the
        hash-skip check is the PDF, not the converted EPUB — and compared against the
        prior pull's stored content hash; a match returns ``upsert=False`` with just the
        refreshed validators/hash as ``custom_values`` (see
        :func:`~ebookerr_sdk.download.document.download_convert_stage`).

        Raises:
            SourcePullError: On download failure, invalid PDF, or conversion failure.
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
            source_suffix=".pdf",
            fmt="pdf",
            convert=convert_pdf_to_epub,
            log_label="PDF",
        )
