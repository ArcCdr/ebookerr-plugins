"""DocxDownloadSourcePlugin — direct .docx URL downloads converted to EPUB.

Takes the title and author from the converted document. Handles direct DOCX downloads from URLs
ending in .docx. Applies site authentication headers (cookies, basic auth, etc.) resolved via the
SPI ``PluginContext.auth_headers``, converts the DOCX to EPUB via convert_docx_to_epub, reads
EPUB metadata, and returns a BookPatch suitable for the core orchestrator.

It also converts an uploaded DOCX file into an EPUB (the converter role, SPI 2.33).

Unlike FanFicFare (priority=1000 catch-all), this plugin claims .docx URLs directly
(priority=100) and skips FanFicFare's dependency chain for pure DOCX downloads.
"""

from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import urlsplit

import requests
from ebookerr_sdk.download.check import check_download_update
from ebookerr_sdk.download.document import convert_stage, download_convert_stage
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    PluginContext,
    SettingsSchema,
    UpdateCheck,
)
from ebookerr_sdk.spi.manifest import package_manifest

from docx_download_source.docx_to_epub import convert_docx_to_epub

__all__ = ["DocxDownloadSourcePlugin"]


class DocxDownloadSourcePlugin:
    """Direct Source plugin for DOCX downloads with site authentication.

    The plugin is a Source: it claims URLs ending in ``.docx`` via ``claims()``
    and claims the ``'docx'`` media format via ``claims_format()``; reports whether
    a newer version exists via ``check_for_update()``; downloads and converts the
    DOCX to EPUB via ``pull()`` and ``convert()``; and declares its settings schema
    via ``settings_schema()``.

    The conversion logic lives in the ``docx_to_epub`` module. The plugin uses SDK
    helpers ``download_convert_stage`` and ``convert_stage`` from
    ``ebookerr_sdk.download.document`` and ``check_download_update`` from
    ``ebookerr_sdk.download.check``. Site authentication headers come from
    ``ctx.auth_headers(url)``.

    Lifecycle and invariants:
        On success, ``pull()`` returns a ``BookPatch`` with title, author, and
        content hash; on failure it raises ``SourcePullError`` or
        ``ContentTypeMismatchError``. Files are staged under the *work_dir*
        parameter. The instance stores the HTTP session and timeout in ``__init__``.
    """

    manifest = package_manifest(__file__)

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
        """Claim absolute URLs whose path ends in ``.docx``.

        Case-insensitive; query and fragment are ignored — the same rule as the
        manifest's ``url_patterns``, which is what the core applies out of process.
        """
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

    def convert(self, path: Path, work_dir: Path, ctx: PluginContext) -> BookPatch:
        """Convert an uploaded DOCX file into an EPUB staged under *work_dir* (SPI 2.33).

        The title and author come from the document's properties, else its first line and
        "Unknown".

        Args:
            path: The uploaded ``.docx`` file; only read.
            work_dir: The folder the EPUB is written into.
            ctx: The plugin context.

        Returns:
            The conversion patch: ``output_filename``, ``title``, ``author``, ``format``
            ``"docx"`` and the ``content_hash``/``content_length`` custom values; ``book_id`` is
            ``""``.

        Raises:
            SourcePullError: The file is not a readable DOCX document or holds no text.
        """
        return convert_stage(
            path,
            work_dir,
            ctx,
            namespace=self.manifest.id,
            fmt="docx",
            convert=convert_docx_to_epub,
            log_label="DOCX",
        )
