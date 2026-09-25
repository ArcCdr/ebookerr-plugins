"""Tests for RtfDownloadSourcePlugin (RTF downloads converted to EPUB)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
import requests
import responses
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.spi import BookPatch, BookView, CustomValueView, InvocationMode
from rtf_download_source.plugin import RtfDownloadSourcePlugin

# RTF fixture: simple valid RTF content
_RTF = r"{\rtf1\ansi\deff0 Hello world.\par\par Second paragraph.\par}"


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


class TestClaimsRtfSuffix:
    def test_claims_rtf(self) -> None:
        """claims() returns True for URLs ending in .rtf."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.rtf") is True

    def test_claims_rtf_uppercase(self) -> None:
        """claims() returns True for URLs ending in .RTF (case-insensitive)."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.RTF") is True

    def test_claims_rtf_mixed_case(self) -> None:
        """claims() returns True for URLs ending in .Rtf (case-insensitive)."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.Rtf") is True

    def test_does_not_claim_txt(self) -> None:
        """claims() returns False for .txt files."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.txt") is False

    def test_does_not_claim_docx(self) -> None:
        """claims() returns False for .docx files."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.docx") is False

    def test_does_not_claim_pdf(self) -> None:
        """claims() returns False for .pdf files."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.pdf") is False


class TestClaimsFormatRtf:
    def test_claims_format_rtf(self) -> None:
        """claims_format('rtf') returns True."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims_format("rtf") is True

    def test_claims_format_txt_false(self) -> None:
        """claims_format('txt') returns False."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims_format("txt") is False

    def test_claims_format_docx_false(self) -> None:
        """claims_format('docx') returns False."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.claims_format("docx") is False


class TestManifestFields:
    def test_manifest_fields(self) -> None:
        """Manifest has correct id and plugin_type."""
        from ebookerr_sdk.spi import PluginType

        plugin = RtfDownloadSourcePlugin()
        assert plugin.manifest.id == "rtf_download_source"
        assert plugin.manifest.plugin_type == PluginType.SOURCE

    def test_manifest_declares_no_events(self) -> None:
        """Manifest declares no events."""
        plugin = RtfDownloadSourcePlugin()
        assert plugin.manifest.events == ()


class TestPullConvertsAndMaps:
    @responses.activate
    def test_pull_converts_and_maps(self, tmp_path: Path) -> None:
        """pull() downloads RTF, converts to EPUB, reads metadata, and returns BookPatch."""
        url = "https://x.com/story.rtf"
        responses.add(
            responses.GET, url, body=_RTF.encode(), status=200, content_type="application/rtf"
        )

        session = requests.Session()
        plugin = RtfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        patch = plugin.pull(url, work_dir, None, ctx)

        assert isinstance(patch, BookPatch)
        assert patch.upsert is True
        assert patch.fields["format"] == "rtf"
        assert patch.fields["output_filename"] == "Unknown/story.epub"

        # Verify the converted EPUB exists and is readable
        epub_path = work_dir / patch.fields["output_filename"]
        assert epub_path.exists()
        epub_doc = EpubDocument.open(epub_path)
        assert epub_doc.content_chapter_count() >= 1


class TestPullEmptyRtfMapsToSourcePullError:
    @responses.activate
    def test_pull_empty_rtf_maps_to_sourcepullerror(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError on empty RTF content."""
        from ebookerr_sdk.spi import SourcePullError

        # Empty RTF with only control words, no text
        empty_rtf = r"{\rtf1\ansi\deff0 \par}"
        url = "https://x.com/story.rtf"
        responses.add(
            responses.GET,
            url,
            body=empty_rtf.encode(),
            status=200,
            content_type="application/rtf",
        )

        session = requests.Session()
        plugin = RtfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError, match="RTF conversion failed"):
            plugin.pull(url, work_dir, None, ctx)


class TestPullHttpError:
    @responses.activate
    def test_pull_http_error(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError on non-200 status."""
        from ebookerr_sdk.spi import SourcePullError

        url = "https://x.com/story.rtf"
        responses.add(responses.GET, url, status=404)

        session = requests.Session()
        plugin = RtfDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError, match="HTTP 404"):
            plugin.pull(url, work_dir, None, ctx)


class TestCheckForUpdateDelegates:
    @responses.activate
    def test_check_for_update_delegates(self) -> None:
        """check_for_update() delegates to helper with correct namespace."""
        from ebookerr_sdk.spi import UpdateCheck as UpdateCheckClass

        url = "https://x.com/book.rtf"

        # Mock a HEAD response
        responses.add(
            responses.HEAD,
            url,
            status=200,
            headers={"ETag": '"abc"'},
        )

        session = requests.Session()
        plugin = RtfDownloadSourcePlugin(session=session)

        # Create prior with stored etag (bare key)
        prior = BookView(
            book_id=make_book_id(url),
            title="Test Book",
            author="Test Author",
            story_url=url,
            output_filename="test.rtf",
            num_chapters=1,
            status=None,
            rating=None,
            cover_ref=None,
            external=None,
            progress=None,
            custom_values={
                "etag": CustomValueView(
                    value="abc",
                    value_type="str",
                    updated_at=None,
                ),
            },
        )

        result = plugin.check_for_update(url, prior=prior)

        # Verify it returns an UpdateCheck with needs_update=False (etag matches)
        assert isinstance(result, UpdateCheckClass)
        assert result.needs_update is False
        assert result.error is None

        # Verify exactly one HEAD call was made
        assert len(responses.calls) == 1
        assert responses.calls[0].request.method == "HEAD"


_CLAIMS_TABLE = [
    # RTF cases (18 rows)
    ("https://example.com/a.rtf", True),
    ("https://example.com/a.RTF", True),
    ("https://example.com/a.Rtf?dl=1", True),
    ("https://example.com/a.rtf#frag", True),
    ("https://example.com/a.rtf?dl=1#frag", True),
    ("https://user@example.com:8080/dir/a.rtf", True),
    ("https://example.com/download?file=a.rtf", False),
    ("https://example.com/?a.rtf", False),
    ("https://example.com/view#a.rtf", False),
    ("https://example.rtf", False),
    ("https://example.rtf?x=1", False),
    ("https://example.rtf/", False),
    ("https://example.com/a.rtf/", False),
    ("https://example.com/a.rtf;v=1", False),
    ("https://example.com/?file=a.rtf&x=1", False),
    ("https://example.com/a%2Ertf", False),
    ("https://example.com/a.rtf.html", False),
    ("https://example.com/a.rtf%20", False),
]


@pytest.mark.parametrize(("url", "claimed"), _CLAIMS_TABLE)
def test_the_manifest_pattern_claims_exactly_what_the_class_claims(url: str, claimed: bool) -> None:
    """The core's out-of-process claim (a regex search of ``url_patterns``) equals the
    class's own rule."""
    pattern = RtfDownloadSourcePlugin.manifest.url_patterns[0]
    assert RtfDownloadSourcePlugin().claims(url) is claimed
    assert (re.search(pattern, url) is not None) is claimed
