"""Tests for PdfDownloadSourcePlugin (PDF downloads converted to EPUB)."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import pymupdf
import pytest
import requests
import responses
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.spi import BookPatch, BookView, CustomValueView, InvocationMode, PluginContext
from pdf_download_source.plugin import PdfDownloadSourcePlugin


def _make_pdf(
    tmp_path: Path,
    pages: list[str],
    toc: list[list] | None = None,
    meta: dict[str, str] | None = None,
) -> Path:
    """Build a synthetic PDF for testing."""
    pdf_path = tmp_path / "test.pdf"
    doc = pymupdf.open()  # type: ignore[no-untyped-call]

    for text in pages:
        page = doc.new_page()  # type: ignore[no-untyped-call]
        if text:
            page.insert_text((72, 72), text, fontsize=11)  # type: ignore[no-untyped-call]

    if toc:
        doc.set_toc(toc)  # type: ignore[no-untyped-call]

    if meta:
        doc.set_metadata(meta)  # type: ignore[no-untyped-call]

    doc.save(pdf_path)  # type: ignore[no-untyped-call]
    doc.close()  # type: ignore[no-untyped-call]

    return pdf_path


class FakeCircuit:
    """A circuit that never opens."""

    def is_open(self, key: str) -> bool:
        """Always closed."""
        return False

    def guard(self, key: str, label: str = "") -> Any:
        """No-op guard context manager."""
        from contextlib import nullcontext

        return nullcontext()


class FakeContext:
    """Minimal mock PluginContext for testing."""

    mode = InvocationMode.HEADLESS
    settings: dict[str, Any] = {}
    ui_context: dict[str, str] = {}
    logger = logging.getLogger("test")
    circuit = FakeCircuit()

    def __init__(self, auth_by_host: dict[str, dict[str, str]] | None = None) -> None:
        """Initialize with optional auth headers by host."""
        self.auth_by_host = auth_by_host or {}

    def report(self, percent: float) -> None:
        """No-op progress reporting."""

    def check_cancelled(self) -> None:
        """No-op cancellation check."""

    def auth_headers(self, url: str) -> dict[str, str]:
        """Return auth headers for the URL's host."""
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname or ""
        return self.auth_by_host.get(host, {})


class TestManifest:
    def test_manifest_declares_no_events(self) -> None:
        """Manifest declares empty events tuple."""
        plugin = PdfDownloadSourcePlugin()
        assert plugin.manifest.events == ()


_CLAIMS_TABLE = [
    ("https://example.com/a.pdf", True),
    ("https://example.com/a.PDF", True),
    ("https://example.com/a.Pdf?dl=1", True),
    ("https://example.com/a.pdf#frag", True),
    ("https://example.com/a.pdf?dl=1#frag", True),
    ("https://user@example.com:8080/dir/a.pdf", True),
    ("https://example.com/download?file=a.pdf", False),
    ("https://example.com/?a.pdf", False),
    ("https://example.com/view#a.pdf", False),
    ("https://example.pdf", False),
    ("https://example.pdf?x=1", False),
    ("https://example.pdf/", False),
    ("https://example.com/a.pdf/", False),
    ("https://example.com/a.pdf;v=1", False),
    ("https://example.com/?file=a.pdf&x=1", False),
    ("https://example.com/a%2Epdf", False),
    ("https://example.com/a.pdf.html", False),
    ("https://example.com/a.pdf%20", False),
]


@pytest.mark.parametrize(("url", "claimed"), _CLAIMS_TABLE)
def test_the_manifest_pattern_claims_exactly_what_the_class_claims(url: str, claimed: bool) -> None:
    """The core's out-of-process claim (a regex search of ``url_patterns``) equals the class's own rule."""
    (pattern,) = PdfDownloadSourcePlugin.manifest.url_patterns
    assert PdfDownloadSourcePlugin().claims(url) is claimed
    assert (re.search(pattern, url) is not None) is claimed


class TestClaimsFormat:
    def test_claims_format_pdf(self) -> None:
        """claims_format('pdf') returns True."""
        plugin = PdfDownloadSourcePlugin()
        assert plugin.claims_format("pdf") is True

    def test_claims_format_non_pdf(self) -> None:
        """claims_format('epub') returns False."""
        plugin = PdfDownloadSourcePlugin()
        assert plugin.claims_format("epub") is False


class TestPullConvertsAndMaps:
    @responses.activate
    def test_pull_converts_and_maps(self, tmp_path: Path) -> None:
        """pull() downloads PDF, converts to EPUB, reads metadata, and returns BookPatch."""
        pdf_path = _make_pdf(
            tmp_path,
            ["Chapter 1 text", "Chapter 2 text"],
            toc=[[1, "Chapter 1", 1], [1, "Chapter 2", 2]],
            meta={"title": "My Book", "author": "Jane"},
        )
        pdf_bytes = pdf_path.read_bytes()

        url = "https://example.com/download.pdf"
        responses.add(
            responses.GET, url, body=pdf_bytes, status=200, content_type="application/pdf"
        )

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        patch = plugin.pull(url, work_dir, None, ctx)

        assert isinstance(patch, BookPatch)
        assert patch.upsert is True
        assert patch.book_id == make_book_id(url)
        assert patch.fields["title"] == "My Book"
        assert patch.fields["author"] == "Jane"
        assert patch.fields["output_filename"] == "Jane/download.epub"
        assert patch.fields["num_chapters"] == 2
        assert patch.fields["auto_pull"] == 0
        assert patch.fields["language"] == "English"
        assert patch.fields["story_url"] == url

        epub_path = work_dir / patch.fields["output_filename"].replace(".pdf", ".epub")
        assert epub_path.exists()
        epub_doc = EpubDocument.open(epub_path)
        meta = epub_doc.read_metadata()
        assert meta.title == "My Book"

        assert not (work_dir / "download.pdf").exists()

    @responses.activate
    def test_pull_uses_stem_as_output_filename(self, tmp_path: Path) -> None:
        """pull() derives pdf filename with .pdf suffix and converts output to .epub."""
        pdf_path = _make_pdf(
            tmp_path,
            ["Content"],
            meta={"title": "Test", "author": "Author"},
        )
        pdf_bytes = pdf_path.read_bytes()

        url = "https://example.com/book.pdf"
        responses.add(
            responses.GET, url, body=pdf_bytes, status=200, content_type="application/pdf"
        )

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.fields["output_filename"].endswith("/book.epub")


class TestPullConversionError:
    @responses.activate
    def test_pull_conversion_error_maps_to_sourcepullerror(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError on PDF conversion failure."""
        from ebookerr_sdk.spi import SourcePullError

        url = "https://example.com/download.pdf"
        responses.add(responses.GET, url, body=b"junk", status=200, content_type="application/pdf")

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError, match="PDF conversion failed"):
            plugin.pull(url, work_dir, None, ctx)


class TestPullHttpError:
    @responses.activate
    def test_pull_http_error_raises_sourcepullerror(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError on non-200 status."""
        from ebookerr_sdk.spi import SourcePullError

        url = "https://example.com/download.pdf"
        responses.add(responses.GET, url, status=404)

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError, match="HTTP 404"):
            plugin.pull(url, work_dir, None, ctx)


class TestBuildFieldsIncludesFormat:
    @responses.activate
    def test_build_fields_includes_format_pdf(self, tmp_path: Path) -> None:
        """pull() returns BookPatch with format='pdf' field."""
        pdf_path = _make_pdf(
            tmp_path,
            ["Chapter 1 text"],
            toc=[[1, "Chapter 1", 1]],
            meta={"title": "My Book", "author": "Author"},
        )
        pdf_bytes = pdf_path.read_bytes()

        url = "https://example.com/download.pdf"
        responses.add(
            responses.GET, url, body=pdf_bytes, status=200, content_type="application/pdf"
        )

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.fields["format"] == "pdf"


class TestCheckForUpdateDelegates:
    @responses.activate
    def test_check_for_update_delegates_to_helper(self) -> None:
        """plugin's check_for_update with prior → delegates to helper."""
        url = "https://example.com/book.pdf"

        responses.add(
            responses.HEAD,
            url,
            status=200,
            headers={"ETag": '"abc"'},
        )

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        prior = BookView(
            book_id=make_book_id(url),
            title="Test Book",
            author="Test Author",
            story_url=url,
            output_filename="test.pdf",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external={"links": [], "progress": {}},
            progress={"sections": [], "chapters": []},
            custom_values={
                "etag": CustomValueView(
                    value="abc",
                    value_type="str",
                    updated_at=None,
                ),
            },
        )

        result = plugin.check_for_update(url, prior=prior)

        assert result.needs_update is False
        assert result.error is None

        assert len(responses.calls) == 1
        assert responses.calls[0].request.method == "HEAD"


class TestPullPersistsValidatorsAndPdfHash:
    @responses.activate
    def test_pull_persists_validators_and_pdf_hash(self, tmp_path: Path) -> None:
        """pull() captures validators (ETag, Last-Modified, Content-Length) and pdf content_hash."""
        pdf_path = _make_pdf(
            tmp_path,
            ["Chapter 1 text"],
            meta={"title": "My Book", "author": "Jane"},
        )
        pdf_bytes = pdf_path.read_bytes()
        expected_hash = hashlib.sha256(pdf_bytes).hexdigest()

        url = "https://example.com/download.pdf"
        responses.add(
            responses.GET,
            url,
            body=pdf_bytes,
            status=200,
            headers={
                "ETag": '"abc"',
                "Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                "Content-Length": str(len(pdf_bytes)),
                "Content-Type": "application/pdf",
            },
        )

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert "etag" in patch.custom_values
        assert "last_modified" in patch.custom_values
        assert "content_length" in patch.custom_values
        assert "content_hash" in patch.custom_values

        for key in ["etag", "content_length", "content_hash"]:
            cv = patch.custom_values[key]
            assert cv.value_type == "str"
            assert cv.value is not None
            assert cv.value != ""

        lm_cv = patch.custom_values["last_modified"]
        assert lm_cv.value_type == "datetime"
        assert lm_cv.value is not None
        assert lm_cv.value != ""

        assert patch.custom_values["etag"].value == "abc"

        assert patch.custom_values["content_hash"].value == expected_hash


class TestPullHashSkipReturnsNoUpdate:
    @responses.activate
    def test_pull_hash_skip_returns_no_update(
        self, tmp_path: Path, monkeypatch: Any, caplog: Any
    ) -> None:
        """pull() with prior whose content_hash matches → upsert=False, no PDF→EPUB conversion."""
        from pdf_download_source import pdf_to_epub

        pdf_path = _make_pdf(
            tmp_path,
            ["Chapter 1 text"],
            meta={"title": "My Book", "author": "Jane"},
        )
        pdf_bytes = pdf_path.read_bytes()
        content_hash = hashlib.sha256(pdf_bytes).hexdigest()

        url = "https://example.com/download.pdf"
        responses.add(
            responses.GET, url, body=pdf_bytes, status=200, content_type="application/pdf"
        )

        session = requests.Session()
        plugin = PdfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        patch1 = plugin.pull(url, work_dir, None, ctx)

        assert patch1.custom_values["content_hash"].value == content_hash

        prior = BookView(
            book_id=make_book_id(url),
            title="My Book",
            author="Jane",
            story_url=url,
            output_filename="Jane/download.epub",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external={"links": [], "progress": {}},
            progress={"sections": [], "chapters": []},
            custom_values={
                "content_hash": CustomValueView(
                    value=content_hash,
                    value_type="str",
                    updated_at=None,
                ),
            },
        )

        conversion_call_count = 0
        original_convert = pdf_to_epub.convert_pdf_to_epub

        def mock_convert(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal conversion_call_count
            conversion_call_count += 1
            return original_convert(*args, **kwargs)

        monkeypatch.setattr("pdf_download_source.plugin.convert_pdf_to_epub", mock_convert)

        import shutil

        shutil.rmtree(work_dir)
        work_dir.mkdir()

        with caplog.at_level(logging.INFO):
            patch2 = plugin.pull(url, work_dir, prior, ctx)

        assert patch2.upsert is False
        assert "unchanged after download (content hash match)" in caplog.text
        assert conversion_call_count == 0
