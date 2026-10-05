"""TextDownloadSourcePlugin — direct .txt/.md URL downloads converted to EPUB.

Handles direct TXT and Markdown downloads from URLs ending in .txt or .md, takes the title
from the text's first line, converts the text to EPUB via convert_text_to_epub, and returns
a BookPatch suitable for the core orchestrator. It also converts an uploaded TXT/Markdown file
into an EPUB (the converter role, SPI 2.33).

Unlike FanFicFare (priority=1000 catch-all), this plugin claims .txt/.md URLs directly
(priority=100) and skips FanFicFare's dependency chain for pure text downloads.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

import requests
from ebookerr_sdk.download.check import check_download_update
from ebookerr_sdk.download.document import convert_stage, download_convert_stage
from ebookerr_sdk.epub.text_convert import convert_text_to_epub
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    PluginContext,
    SettingsSchema,
    UpdateCheck,
)
from ebookerr_sdk.spi.manifest import package_manifest

__all__ = ["TextDownloadSourcePlugin"]

logger = logging.getLogger(__name__)


class TextDownloadSourcePlugin:
    """Direct Source plugin for TXT/Markdown downloads with site authentication.

    This plugin is a Source: it claims URLs ending in ``.txt`` or ``.md`` (``claims``,
    ``claims_format``), reports whether a newer version exists (``check_for_update``),
    downloads the file (``pull``), declares its settings (``settings_schema``), and
    converts the text to EPUB (``convert``). The conversion is performed by the
    ``convert_text_to_epub`` function from the SDK.

    The SDK helpers ``download_convert_stage`` and ``convert_stage`` orchestrate the
    full download-convert-stage workflow, and ``check_download_update`` backs
    ``check_for_update``. Site authentication headers are resolved via
    ``PluginContext.auth_headers()`` based on the target URL profile.

    Lifecycle and invariants:
        The ``pull()`` method returns a ``BookPatch`` on success; on failure it raises
        ``SourcePullError`` (download or conversion) or ``ContentTypeMismatchError``. The
        ``convert()`` method also returns a ``BookPatch``. The instance stores the HTTP
        session and timeout in ``__init__``.
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
        """Claim absolute URLs whose path ends in ``.txt`` or ``.md``.

        Case-insensitive; query and fragment are ignored — the same rule as the
        manifest's ``url_patterns``, which is what the core applies out of process.
        """
        path = urlsplit(url).path.lower()
        return path.endswith((".txt", ".md"))

    def claims_format(self, media_format: str) -> bool:
        """Claim the 'txt' media format (both .txt and .md use this format token)."""
        return media_format == "txt"

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
        """Download TXT/MD, convert to EPUB, stage file, and return BookPatch.

        Raises SourcePullError on download failure, invalid text, or conversion failure.
        """
        # Determine suffix based on URL path
        suffix = ".md" if urlsplit(url).path.lower().endswith(".md") else ".txt"

        return download_convert_stage(
            url,
            work_dir,
            prior,
            ctx,
            session=self._session,
            auth_headers=ctx.auth_headers(url),
            timeout_s=self._timeout_s,
            namespace=self.manifest.id,
            source_suffix=suffix,
            fmt="txt",
            convert=convert_text_to_epub,
            log_label="Text",
        )

    def convert(self, path: Path, work_dir: Path, ctx: PluginContext) -> BookPatch:
        """Convert an uploaded TXT or Markdown file into an EPUB staged under *work_dir* (SPI 2.33).

        The title comes from the text's first line, else the file name; the author is "Unknown".

        Args:
            path: The uploaded ``.txt`` or ``.md`` file; only read.
            work_dir: The folder the EPUB is written into.
            ctx: The plugin context.

        Returns:
            The conversion patch: ``output_filename``, ``title``, ``author``, ``format`` ``"txt"``
            and the ``content_hash``/``content_length`` custom values; ``book_id`` is ``""``.

        Raises:
            SourcePullError: The file holds no text.
        """
        return convert_stage(
            path,
            work_dir,
            ctx,
            namespace=self.manifest.id,
            fmt="txt",
            convert=convert_text_to_epub,
            log_label="Text",
        )
