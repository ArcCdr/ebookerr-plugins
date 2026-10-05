"""RtfDownloadSourcePlugin — direct .rtf URL downloads converted to EPUB.

Handles direct RTF downloads from URLs ending in .rtf. Applies site authentication
headers (cookies, basic auth, etc.) resolved via the SPI ``PluginContext.auth_headers``,
converts the RTF to EPUB via convert_rtf_to_epub, takes the title from the document's
first line, and returns a BookPatch suitable for the core orchestrator.

It also converts an uploaded RTF file into an EPUB (the converter role, SPI 2.33).

Unlike FanFicFare (priority=1000 catch-all), this plugin claims .rtf URLs directly
(priority=100) and skips FanFicFare's dependency chain for pure RTF downloads.
"""

from __future__ import annotations

import logging
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

from rtf_download_source.rtf_to_epub import convert_rtf_to_epub

__all__ = ["RtfDownloadSourcePlugin"]

logger = logging.getLogger(__name__)


class RtfDownloadSourcePlugin:
    """Direct Source plugin for RTF downloads with site authentication.

    The plugin is a Source: it claims URLs ending in ``.rtf`` via ``claims()``
    and claims the ``'rtf'`` media format via ``claims_format()``; reports whether
    a newer version exists via ``check_for_update()``; downloads and converts the
    RTF to EPUB via ``pull()`` and ``convert()``; and declares its settings schema
    via ``settings_schema()``.

    The conversion logic lives in the ``rtf_to_epub`` module. The plugin uses SDK
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
        self, url: str, *, prior: BookView | None, ctx: PluginContext
    ) -> UpdateCheck:
        """Check for update via HEAD request and validator comparison.

        The HEAD request carries no site credentials.

        Args:
            url: The book's source URL.
            prior: The book's current snapshot, or ``None`` when unknown.
            ctx: The call's plugin context (unused by this check).

        Returns:
            The validator comparison's verdict.
        """
        return check_download_update(
            url,
            session=self._session,
            auth_headers={},
            prior=prior,
            namespace=self.manifest.id,
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

    def convert(self, path: Path, work_dir: Path, ctx: PluginContext) -> BookPatch:
        """Convert an uploaded RTF file into an EPUB staged under *work_dir* (SPI 2.33).

        The title comes from the document's first line, else the file name.

        Args:
            path: The uploaded ``.rtf`` file; only read.
            work_dir: The folder the EPUB is written into.
            ctx: The plugin context.

        Returns:
            The conversion patch: ``output_filename``, ``title``, ``author``, ``format`` ``"rtf"``
            and the ``content_hash``/``content_length`` custom values; ``book_id`` is ``""``.

        Raises:
            SourcePullError: The file is not a readable RTF document or holds no text.
        """
        return convert_stage(
            path,
            work_dir,
            ctx,
            namespace=self.manifest.id,
            fmt="rtf",
            convert=convert_rtf_to_epub,
            log_label="RTF",
        )
