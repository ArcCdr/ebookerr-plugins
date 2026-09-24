"""EpubDownloadSourcePlugin — direct .epub downloads with site authentication.

Handles direct EPUB downloads from URLs ending in .epub. Applies site authentication
headers (cookies, basic auth, etc.) resolved via the SPI ``PluginContext.auth_headers``,
reads EPUB metadata via EpubDocument, and returns a BookPatch suitable for the core orchestrator.

Unlike FanFicFare (priority=1000 catch-all), this plugin claims .epub URLs directly
(priority=100) and skips FanFicFare's dependency chain for pure EPUB downloads. Its
returned patch carries real ``fields`` (title, author, ``output_filename``, ...) —
unlike FanFicFare's self-persisted, empty-``fields`` patch — so it flows through the
ordinary ``apply_book_patch`` create-from-fields path, including the unique-filename
finalization step that renames on a collision.

**Two-layer update detection**: :func:`~ebookerr_sdk.download.check.check_download_update`
gives the Auto-Pull scan a cheap pre-download signal from HTTP validators (ETag /
Last-Modified / Content-Length); ``pull()`` itself adds a second, authoritative layer
after the bytes are actually in hand — see its docstring for the content-hash skip.
"""

from __future__ import annotations

import logging
import threading
import zipfile
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import requests
from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.domain.epub_hash import epub_content_hash
from ebookerr_sdk.domain.epub_metadata import language_name
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.domain.metadata import sanitize
from ebookerr_sdk.download.check import (
    check_download_update,
    download_failure_message,
    normalise_validator,
)
from ebookerr_sdk.download.disposition import parse_content_disposition_filename
from ebookerr_sdk.download.paths import safe_component as _safe_component
from ebookerr_sdk.epub import EpubDocument, EpubError, MalformedEpubError
from ebookerr_sdk.spi import (
    BookPatch,
    BookView,
    ChapterLink,
    CircuitGuard,
    CircuitOpenError,
    ContentTypeMismatchError,
    CustomValueWrite,
    PluginContext,
    PluginManifest,
    PluginType,
    SettingsSchema,
    SourcePullError,
    UpdateCheck,
)

if TYPE_CHECKING:
    pass

__all__ = ["EpubDownloadSourcePlugin"]

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_MANIFEST = PluginManifest(
    id="epub_download_source",
    name="EPUB download",
    version="1.1.0",
    plugin_type=PluginType.SOURCE,
    settings_schema=SettingsSchema(),
    priority=100,
    events=(),
    url_patterns=(r"(?i)^[^:/?#]+://[^/?#]*/[^?#]*\.epub(?:[?#]|$)",),
    description="Downloads a story EPUB from a direct download URL.",
    update_check=True,
    default_enabled=True,
    run_timeout_s=900,
    icon="download",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_download_source",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_download_source",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    formats=("epub",),
)


def _extract_filename(content_disposition: str, response_url: str, request_url: str) -> str:
    """Extract filename from Content-Disposition or fallback to URL path (percent-decoded).

    When URL fallback is used, the decoded filename is passed through safe_component to
    prevent directory traversal or invalid filesystem characters.

    Args:
        content_disposition: Content-Disposition header value, or empty string.
        response_url: The response URL (may differ from request URL due to redirects).
        request_url: The original request URL.

    Returns:
        A safe filename ending in .epub.
    """
    filename = parse_content_disposition_filename(content_disposition)
    if not filename:
        # Fallback to URL path; percent-decode to preserve real characters
        url_name = Path(urlsplit(response_url).path).name or Path(urlsplit(request_url).path).name
        if url_name:
            filename = unquote(url_name)
    if not filename:
        filename = "book.epub"
    # Apply safe_component to the entire filename to prevent directory traversal and sanitize
    filename = _safe_component(filename)
    if not filename.lower().endswith(".epub"):
        filename += ".epub"
    return filename


def _guard(circuit: CircuitGuard | None, key: str, host: str) -> AbstractContextManager[None]:
    """The breaker around one download, or a no-op without a circuit or a host.

    Args:
        circuit: The app's shared circuit breakers, or None.
        key: The registry key (e.g. "host:example.com").
        host: The hostname for display, or "" for no guarding.

    Returns:
        A context manager that guards the GET request, or nullcontext() when unguarded.
    """
    if circuit is None or not host:
        return nullcontext()
    return circuit.guard(key, label=host)


class EpubDownloadSourcePlugin:
    """Direct Source plugin for EPUB downloads with site authentication."""

    manifest = _MANIFEST

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        """Store the HTTP session.

        A fresh ``requests.Session`` when none is given (the plugin
        process serves one call) — and the request timeout.
        """
        self._session = session if session is not None else requests.Session()
        self._timeout_s = timeout_s

    def settings_schema(self) -> SettingsSchema:
        """Return the (empty) settings schema — this plugin has no user-configurable options."""
        return SettingsSchema()

    def claims(self, url: str) -> bool:
        """Claim absolute URLs whose path ends in .epub.

        Any case; query and fragment ignored — the same rule as the
        manifest's ``url_patterns``, which is what the core applies out of process.
        """
        path = urlsplit(url).path.lower()
        return path.endswith(".epub")

    def claims_format(self, media_format: str) -> bool:
        """Claim the 'epub' media format."""
        return media_format == "epub"

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

    def pull(  # noqa: C901 — sequential checks: HEAD skip, circuit guard, status/content-type/hash
        self,
        url: str,
        work_dir: Path,
        prior: BookView | None,
        ctx: PluginContext,
    ) -> BookPatch:
        """Download EPUB, read metadata, stage file, and return BookPatch.

        Captures the response's ``ETag``/``Last-Modified``/``Content-Length`` validators
        and, after staging, hashes the downloaded EPUB's content
        (:func:`~ebookerr_sdk.domain.epub_hash.epub_content_hash`). When that hash matches the
        prior pull's stored ``<plugin_id>.content_hash`` custom value — a HEAD validator
        can drift (e.g. a re-served ``Last-Modified``) without the bytes actually
        changing — this is logged as an unchanged-content skip and the returned patch
        carries ``upsert=False`` with just the refreshed validators/hash as
        ``custom_values`` (no ``fields``), so the orchestrator treats it as a no-op
        exactly like FanFicFare's no-new-content skip. Otherwise every ``BookPatch``
        (skip or not) carries the freshly re-captured validators as ``custom_values``, so
        the next :func:`~ebookerr_sdk.download.check.check_download_update` call always
        compares against the most recent pull, never the original one.

        The patch declares the single chapter this download produced (``chapters``), whose
        URL is the download URL itself — a one-file download is one chapter at one address.

        Raises:
            SourcePullError: On download failure, invalid EPUB, or too-large file.
            ContentTypeMismatchError: When the server's Content-Type contradicts the claimed format.
        """
        from ebookerr_sdk.download.routing import response_format_mismatch

        # HEAD-first check: if prior exists, try a HEAD request to detect unchanged content
        if prior is not None:
            check = check_download_update(
                url,
                session=self._session,
                auth_headers=ctx.auth_headers(url),
                prior=prior,
                namespace=self.manifest.id,
            )
            if check.error is None and not check.needs_update:
                logger.info(
                    '"%s" unchanged per HEAD validators — skipped without downloading the body',
                    url,
                )
                # Return a skip patch with validators from the prior state
                # A plugin receives its own custom values bare-keyed (the wire strips the namespace)
                custom_values: dict[str, CustomValueWrite] = {}
                for k in ("etag", "last_modified", "content_length"):
                    cv = prior.custom_values.get(k)
                    if cv is not None:
                        custom_values[k] = CustomValueWrite(
                            value=cv.value,
                            value_type="datetime" if k == "last_modified" else "str",
                        )
                cv_hash = prior.custom_values.get("content_hash")
                if cv_hash is not None:
                    custom_values["content_hash"] = CustomValueWrite(
                        value=cv_hash.value,
                        value_type="str",
                    )
                return BookPatch(
                    book_id=make_book_id(url), upsert=False, custom_values=custom_values
                )

        headers = {"User-Agent": _UA, **ctx.auth_headers(url)}

        # Extract host for circuit breaker keying
        host = urlsplit(url).hostname or ""
        circuit_key = f"host:{host}"
        guard = getattr(ctx, "circuit", None)

        # Check if circuit is open; if so, skip with fail-closed
        if guard is not None and host and guard.is_open(circuit_key):
            logger.info("Download skipped: %s is in a failure back-off (url=%s)", host, url)
            raise SourcePullError(f"{host} is not reachable; retrying automatically")

        # Download
        try:
            with _guard(guard, circuit_key, host):
                resp = self._session.get(
                    url,
                    headers=headers,
                    timeout=(10, self._timeout_s),
                    stream=True,
                    allow_redirects=True,
                )
        except CircuitOpenError:
            raise SourcePullError(f"{host} is not reachable; retrying automatically") from None
        except requests.RequestException as exc:
            logger.warning("Download failed for %s: %s — retrying", url, exc)
            raise SourcePullError(f"Download failed: {exc}") from exc

        if resp.status_code != 200:
            raise SourcePullError(download_failure_message(resp.status_code))

        detected = response_format_mismatch(resp.headers.get("Content-Type"), "epub")
        if detected is not None:
            logger.warning(
                "Content-type mismatch for %s: claimed epub, server says %s — refusing to convert",
                url,
                detected,
            )
            raise ContentTypeMismatchError(
                f"the server says this is {detected}, not epub", detected_format=detected
            )

        # Capture validators for update checking
        validators = {
            "etag": (resp.headers.get("ETag") or "").removeprefix("W/").strip('"'),
            "last_modified": resp.headers.get("Last-Modified") or "",
            "content_length": resp.headers.get("Content-Length") or "",
        }

        filename = _extract_filename(
            resp.headers.get("Content-Disposition", ""), str(resp.url), url
        )
        downloaded_size = self._download_to_file(resp, work_dir)
        ctx.report(40.0)

        meta = self._read_epub_metadata(work_dir)
        author = sanitize(meta.author) or "Unknown"
        title = sanitize(meta.title) or Path(filename).stem

        output_filename = f"{_safe_component(author)}/{filename}"
        output_path = work_dir / output_filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        (work_dir / "download.part").rename(output_path)

        # Hash-skip: compare against prior's stored content_hash
        new_hash = epub_content_hash(output_path)
        prior_hash = None
        if prior is not None:
            cv = prior.custom_values.get("content_hash")
            prior_hash = str(cv.value) if cv is not None and cv.value else None
        if prior_hash is not None and new_hash == prior_hash:
            logger.info('"%s" unchanged after download (content hash match) — no update', title)
            custom_values = {
                k: CustomValueWrite(
                    value=normalise_validator(k, v),
                    value_type="datetime" if k == "last_modified" else "str",
                )
                for k, v in validators.items()
                if v
            }
            custom_values["content_hash"] = CustomValueWrite(value=new_hash, value_type="str")
            return BookPatch(book_id=make_book_id(url), upsert=False, custom_values=custom_values)

        fields = self._build_fields(url, meta, title, author, output_filename)
        logger.info('Downloaded EPUB "%s" (%d bytes) from %s', title, downloaded_size, url)
        ctx.report(60.0)

        # Build custom_values with validators and content_hash
        custom_values = {
            k: CustomValueWrite(
                value=normalise_validator(k, v),
                value_type="datetime" if k == "last_modified" else "str",
            )
            for k, v in validators.items()
            if v
        }
        custom_values["content_hash"] = CustomValueWrite(value=new_hash, value_type="str")

        return BookPatch(
            book_id=make_book_id(url),
            fields=fields,
            custom_values=custom_values,
            upsert=True,
            chapters=(ChapterLink(url=url, title=title),),
        )

    def _download_to_file(self, resp: requests.Response, work_dir: Path) -> int:
        """Download response stream to file with size cap. Returns bytes written."""
        part_path = work_dir / "download.part"
        downloaded_size = 0
        max_size = 512 * 1024 * 1024

        try:
            with part_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if downloaded_size > max_size:
                            raise SourcePullError("download too large")
        except SourcePullError:
            raise
        except Exception as e:
            raise SourcePullError(f"download failed: {e}") from e

        return downloaded_size

    def _read_epub_metadata(self, work_dir: Path) -> Any:
        """Read EPUB metadata from the downloaded file."""
        part_path = work_dir / "download.part"
        try:
            return EpubDocument.open(part_path).read_metadata()
        except (EpubError, MalformedEpubError, zipfile.BadZipFile) as e:
            raise SourcePullError("downloaded file is not a valid EPUB") from e

    def _build_fields(
        self,
        url: str,
        meta: Any,
        title: str,
        author: str,
        output_filename: str,
    ) -> dict[str, Any]:
        """Build BookPatch fields from metadata.

        The EPUB's ``dc:date`` values are parsed into timezone-aware UTC datetimes
        here; an unparseable value is omitted rather than stored.
        """
        fields = {
            "title": title,
            "author": author,
            "story_url": url,
            "site": urlsplit(url).hostname,
            "description": sanitize(meta.description),
            "language": language_name(meta.language) or "English",
            "date_published": parse_datetime(meta.date_published),
            "date_updated": parse_datetime(meta.date_updated),
            "status": sanitize(meta.status),
            "num_chapters": meta.chapter_count,
            "output_filename": output_filename,
            "format": "epub",
            "auto_pull": 0,
        }
        return {k: v for k, v in fields.items() if v is not None}
