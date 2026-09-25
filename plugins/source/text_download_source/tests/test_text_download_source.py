"""Tests for TextDownloadSourcePlugin (TXT/MD downloads converted to EPUB)."""

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
from text_download_source.plugin import TextDownloadSourcePlugin


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


class TestClaimsTxtAndMd:
    def test_claims_txt(self) -> None:
        """claims() returns True for URLs ending in .txt."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.txt") is True

    def test_claims_md(self) -> None:
        """claims() returns True for URLs ending in .md."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.md") is True

    def test_claims_md_uppercase(self) -> None:
        """claims() returns True for URLs ending in .MD (case-insensitive)."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.MD") is True

    def test_does_not_claim_docx(self) -> None:
        """claims() returns False for .docx files."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.docx") is False

    def test_does_not_claim_pdf(self) -> None:
        """claims() returns False for .pdf files."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims("https://x.com/a.pdf") is False


class TestClaimsFormatTxt:
    def test_claims_format_txt(self) -> None:
        """claims_format('txt') returns True."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims_format("txt") is True

    def test_claims_format_md_false(self) -> None:
        """claims_format('md') returns False."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims_format("md") is False

    def test_claims_format_docx_false(self) -> None:
        """claims_format('docx') returns False."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.claims_format("docx") is False


class TestManifestFields:
    def test_manifest_fields(self) -> None:
        """Manifest has correct id and plugin_type."""
        from ebookerr_sdk.spi import PluginType

        plugin = TextDownloadSourcePlugin()
        assert plugin.manifest.id == "text_download_source"
        assert plugin.manifest.plugin_type == PluginType.SOURCE

    def test_manifest_declares_no_events(self) -> None:
        """Manifest declares no events."""
        plugin = TextDownloadSourcePlugin()
        assert plugin.manifest.events == ()


class TestPullTxtConvertsAndMaps:
    @responses.activate
    def test_pull_txt_converts_and_maps(self, tmp_path: Path) -> None:
        """pull() downloads TXT, converts to EPUB, reads metadata, and returns BookPatch."""
        text_content = b"# My Story\n\nhello world\n"
        url = "https://x.com/story.txt"
        responses.add(responses.GET, url, body=text_content, status=200)

        session = requests.Session()
        plugin = TextDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        patch = plugin.pull(url, work_dir, None, ctx)

        assert isinstance(patch, BookPatch)
        assert patch.upsert is True
        assert patch.fields["format"] == "txt"
        assert patch.fields["title"] == "My Story"
        assert patch.fields["output_filename"] == "Unknown/story.epub"

        # Verify the converted EPUB exists and is readable
        epub_path = work_dir / patch.fields["output_filename"]
        assert epub_path.exists()
        epub_doc = EpubDocument.open(epub_path)
        assert epub_doc.content_chapter_count() >= 1


class TestPullMdConvertsAndMaps:
    @responses.activate
    def test_pull_md_converts_and_maps(self, tmp_path: Path) -> None:
        """pull() downloads MD, converts to EPUB, reads metadata, and returns BookPatch."""
        text_content = b"# Ch1\n\ntext\n\n# Ch2\n\nmore\n"
        url = "https://x.com/story.md"
        responses.add(responses.GET, url, body=text_content, status=200)

        session = requests.Session()
        plugin = TextDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        patch = plugin.pull(url, work_dir, None, ctx)

        assert isinstance(patch, BookPatch)
        assert patch.fields["format"] == "txt"

        # Verify the converted EPUB exists and has 2 chapters
        epub_path = work_dir / patch.fields["output_filename"]
        assert epub_path.exists()
        epub_doc = EpubDocument.open(epub_path)
        assert epub_doc.content_chapter_count() == 2


class TestPullEmptyTextMapsToSourcePullError:
    @responses.activate
    def test_pull_empty_text_maps_to_sourcepullerror(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError on empty text content."""
        from ebookerr_sdk.spi import SourcePullError

        text_content = b"   \n\n"
        url = "https://x.com/story.txt"
        responses.add(responses.GET, url, body=text_content, status=200)

        session = requests.Session()
        plugin = TextDownloadSourcePlugin(session=session)

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError, match="Text conversion failed"):
            plugin.pull(url, work_dir, None, ctx)


class TestPullHttpError:
    @responses.activate
    def test_pull_http_error(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError on non-200 status."""
        from ebookerr_sdk.spi import SourcePullError

        url = "https://x.com/story.txt"
        responses.add(responses.GET, url, status=404)

        session = requests.Session()
        plugin = TextDownloadSourcePlugin(session=session)

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

        url = "https://x.com/book.txt"

        # Mock a HEAD response
        responses.add(
            responses.HEAD,
            url,
            status=200,
            headers={"ETag": '"abc"'},
        )

        session = requests.Session()
        plugin = TextDownloadSourcePlugin(session=session)

        # Create prior with stored etag
        prior = BookView(
            book_id=make_book_id(url),
            title="Test Book",
            author="Test Author",
            story_url=url,
            output_filename="test.txt",
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
    # TXT cases (18 rows from EPUB download source, adapted for .txt)
    ("https://example.com/a.txt", True),
    ("https://example.com/a.TXT", True),
    ("https://example.com/a.Txt?dl=1", True),
    ("https://example.com/a.txt#frag", True),
    ("https://example.com/a.txt?dl=1#frag", True),
    ("https://user@example.com:8080/dir/a.txt", True),
    ("https://example.com/download?file=a.txt", False),
    ("https://example.com/?a.txt", False),
    ("https://example.com/view#a.txt", False),
    ("https://example.txt", False),
    ("https://example.txt?x=1", False),
    ("https://example.txt/", False),
    ("https://example.com/a.txt/", False),
    ("https://example.com/a.txt;v=1", False),
    ("https://example.com/?file=a.txt&x=1", False),
    ("https://example.com/a%2Etxt", False),
    ("https://example.com/a.txt.html", False),
    ("https://example.com/a.txt%20", False),
    # MD cases (18 rows, same patterns for .md)
    ("https://example.com/a.md", True),
    ("https://example.com/a.MD", True),
    ("https://example.com/a.Md?dl=1", True),
    ("https://example.com/a.md#frag", True),
    ("https://example.com/a.md?dl=1#frag", True),
    ("https://user@example.com:8080/dir/a.md", True),
    ("https://example.com/download?file=a.md", False),
    ("https://example.com/?a.md", False),
    ("https://example.com/view#a.md", False),
    ("https://example.md", False),
    ("https://example.md?x=1", False),
    ("https://example.md/", False),
    ("https://example.com/a.md/", False),
    ("https://example.com/a.md;v=1", False),
    ("https://example.com/?file=a.md&x=1", False),
    ("https://example.com/a%2Emd", False),
    ("https://example.com/a.md.html", False),
    ("https://example.com/a.md%20", False),
]


@pytest.mark.parametrize(("url", "claimed"), _CLAIMS_TABLE)
def test_the_manifest_pattern_claims_exactly_what_the_class_claims(url: str, claimed: bool) -> None:
    """The core's out-of-process claim (a regex search of ``url_patterns``) equals the
    class's own rule."""
    pattern = TextDownloadSourcePlugin.manifest.url_patterns[0]
    assert TextDownloadSourcePlugin().claims(url) is claimed
    assert (re.search(pattern, url) is not None) is claimed
