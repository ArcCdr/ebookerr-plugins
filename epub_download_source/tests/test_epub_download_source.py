"""Tests for EpubDownloadSourcePlugin (direct .epub downloads with site auth)."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
import responses
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.spi import (
    BookView,
    ChapterLink,
    CustomValueView,
    CustomValueWrite,
    ExternalLink,
    ExternalProgress,
    InvocationMode,
    PluginContext,
)
from ebookerr_sdk.testing import make_book_view

from epub_download_source.plugin import EpubDownloadSourcePlugin, _extract_filename, _safe_component


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


class TestPluginConstruction:
    def test_no_site_auth_parameter(self) -> None:
        """Plugin constructor takes no site_auth parameter."""
        import inspect

        sig = inspect.signature(EpubDownloadSourcePlugin.__init__)
        assert "site_auth" not in sig.parameters, "site_auth should not be in plugin constructor"

    def test_plugin_takes_session_and_timeout(self) -> None:
        """Plugin constructor accepts optional session and timeout_s."""
        plugin = EpubDownloadSourcePlugin()
        assert plugin is not None

        plugin2 = EpubDownloadSourcePlugin(session=requests.Session())
        assert plugin2 is not None


class TestManifest:
    def test_manifest_declares_no_events(self) -> None:
        """Manifest declares empty events tuple."""
        plugin = EpubDownloadSourcePlugin()
        assert plugin.manifest.events == ()


_CLAIMS_TABLE = [
    ("https://example.com/a.epub", True),
    ("https://example.com/a.EPUB", True),
    ("https://example.com/a.Epub?dl=1", True),
    ("https://example.com/a.epub#frag", True),
    ("https://example.com/a.epub?dl=1#frag", True),
    ("https://user@example.com:8080/dir/a.epub", True),
    ("https://example.com/download?file=a.epub", False),
    ("https://example.com/?a.epub", False),
    ("https://example.com/view#a.epub", False),
    ("https://example.epub", False),
    ("https://example.epub?x=1", False),
    ("https://example.epub/", False),
    ("https://example.com/a.epub/", False),
    ("https://example.com/a.epub;v=1", False),
    ("https://example.com/?file=a.epub&x=1", False),
    ("https://example.com/a%2Eepub", False),
    ("https://example.com/a.epub.html", False),
    ("https://example.com/a.epub%20", False),
]


@pytest.mark.parametrize(("url", "claimed"), _CLAIMS_TABLE)
def test_the_manifest_pattern_claims_exactly_what_the_class_claims(url: str, claimed: bool) -> None:
    """The core's out-of-process claim (a regex search of ``url_patterns``) equals the class's own rule."""
    (pattern,) = EpubDownloadSourcePlugin.manifest.url_patterns
    assert EpubDownloadSourcePlugin().claims(url) is claimed
    assert (re.search(pattern, url) is not None) is claimed


class TestClaimsFormat:
    def test_claims_format_epub(self) -> None:
        """claims_format('epub') returns True."""
        plugin = EpubDownloadSourcePlugin()
        assert plugin.claims_format("epub") is True

    def test_claims_format_non_epub(self) -> None:
        """claims_format('pdf') returns False."""
        plugin = EpubDownloadSourcePlugin()
        assert plugin.claims_format("pdf") is False


class TestPullDownloadsAndMapsMetadata:
    @responses.activate
    def test_pull_downloads_and_maps_metadata(self, tmp_path: Path, build_epub: Any) -> None:
        """pull() downloads EPUB, reads metadata, and returns BookPatch with correct fields."""
        # Build a tiny EPUB
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.upsert is True
        assert patch.book_id == make_book_id(url)
        assert "output_filename" in patch.fields
        assert patch.fields["auto_pull"] == 0
        assert patch.fields["language"] == "English"
        assert "title" in patch.fields
        assert "author" in patch.fields


class TestPullSendContextAuthHeaders:
    @responses.activate
    def test_pull_sends_the_context_auth_headers(self, tmp_path: Path, build_epub: Any) -> None:
        """pull() includes auth headers from context.auth_headers()."""
        # Build a tiny EPUB
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://www.example.com/file/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext(auth_by_host={"www.example.com": {"Cookie": "a=b"}})
        plugin.pull(url, work_dir, None, ctx)

        # Verify the request had the Cookie header and User-Agent
        assert len(responses.calls) == 1
        assert responses.calls[0].request.headers["Cookie"] == "a=b"
        assert "User-Agent" in responses.calls[0].request.headers

    @responses.activate
    def test_pull_with_no_auth_sends_only_the_user_agent(
        self, tmp_path: Path, build_epub: Any
    ) -> None:
        """pull() sends User-Agent even when context returns empty auth headers."""
        # Build a tiny EPUB
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://www.example.com/file/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        plugin.pull(url, work_dir, None, ctx)

        # Verify the request has User-Agent but no Cookie/Authorization
        assert len(responses.calls) == 1
        assert "User-Agent" in responses.calls[0].request.headers
        assert "Cookie" not in responses.calls[0].request.headers
        assert "Authorization" not in responses.calls[0].request.headers


class TestPullHttpError:
    @responses.activate
    def test_pull_http_error_raises_sourcepullerror(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError on non-200 status."""
        from ebookerr_sdk.spi import SourcePullError

        url = "https://example.com/download.epub"
        responses.add(responses.GET, url, status=403)

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError, match="HTTP 403"):
            plugin.pull(url, work_dir, None, ctx)

    @responses.activate
    def test_pull_403_explains_the_refusal(self, tmp_path: Path) -> None:
        """pull() explains a 403 refusal instead of a bare status code."""
        from ebookerr_sdk.spi import SourcePullError

        url = "https://example.com/download.epub"
        responses.add(responses.GET, url, status=403)

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError) as exc_info:
            plugin.pull(url, work_dir, None, ctx)
        assert str(exc_info.value) == (
            "download refused: HTTP 403 — the site denied access"
            " (the sign-in may have expired, or access to this item has ended)"
        )


class TestPullInvalidEpub:
    @responses.activate
    def test_pull_invalid_epub_raises(self, tmp_path: Path) -> None:
        """pull() raises SourcePullError when downloaded file is not a valid EPUB."""
        from ebookerr_sdk.spi import SourcePullError

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=b"not a zip",
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(SourcePullError, match="not a valid EPUB"):
            plugin.pull(url, work_dir, None, ctx)


class TestPullNavOnlyEpub3:
    @responses.activate
    def test_pull_accepts_epub3_nav_only_file(self, tmp_path: Path, build_nav_epub: Any) -> None:
        """pull() succeeds for a spec-valid EPUB3 with only nav.xhtml (no toc.ncx)."""
        epub_path = build_nav_epub([("Chapter 1", "u1")], doc_title="AIF 36", author="Creator")
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.upsert is True
        assert patch.fields["title"] == "AIF 36"
        assert patch.fields["author"] == "Creator"
        assert patch.fields["num_chapters"] == 1


class TestPullContentDispositionFilename:
    @responses.activate
    def test_pull_content_disposition_filename_wins(self, tmp_path: Path, build_epub: Any) -> None:
        """pull() uses filename from Content-Disposition header when present."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={
                "Content-Disposition": 'attachment; filename="My Story.epub"',
                "Content-Type": "application/epub+zip",
            },
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.fields["output_filename"].endswith("/My Story.epub")


class TestPullMissingAuthor:
    @responses.activate
    def test_pull_missing_author_uses_unknown_dir(self, tmp_path: Path, build_epub: Any) -> None:
        """pull() uses 'Unknown' as author directory when EPUB has no author."""
        # Build EPUB without author
        epub_path = build_epub(
            [("Chapter 1", "http://example.com/ch1")],
            author="",
        )
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.fields["output_filename"].startswith("Unknown/")


class TestPullLogsInfo:
    @responses.activate
    def test_pull_logs_info(self, tmp_path: Path, build_epub: Any, caplog: Any) -> None:
        """pull() logs INFO message with title and URL."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with caplog.at_level(logging.INFO):
            plugin.pull(url, work_dir, None, ctx)

        assert 'Downloaded EPUB "' in caplog.text
        assert url in caplog.text


class TestBuildFieldsIncludesFormat:
    @responses.activate
    def test_build_fields_includes_format_epub(self, tmp_path: Path, build_epub: Any) -> None:
        """pull() returns BookPatch with format='epub' field."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.fields["format"] == "epub"


class TestPullPersistsValidatorsAndHash:
    @responses.activate
    def test_pull_persists_validators_and_hash(self, tmp_path: Path, build_epub: Any) -> None:
        """pull() captures ETag, Last-Modified, Content-Length, and content_hash in
        custom_values."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={
                "ETag": '"abc"',
                "Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                "Content-Length": str(len(epub_bytes)),
                "Content-Type": "application/epub+zip",
            },
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        # Verify custom_values contains validators and content_hash (bare-keyed)
        assert "etag" in patch.custom_values
        assert "last_modified" in patch.custom_values
        assert "content_length" in patch.custom_values
        assert "content_hash" in patch.custom_values

        # Verify value_type is "str" for the opaque tokens and "datetime" for last_modified
        for key in ["etag", "content_length", "content_hash"]:
            cv = patch.custom_values[key]
            assert cv.value_type == "str"
            assert cv.value is not None
            assert cv.value != ""

        lm_cv = patch.custom_values["last_modified"]
        assert lm_cv.value_type == "datetime"
        assert lm_cv.value is not None
        assert lm_cv.value != ""

        # Verify etag value is stripped (no quotes, no W/ prefix)
        assert patch.custom_values["etag"].value == "abc"


class TestPullHashSkipReturnsNoUpdate:
    @responses.activate
    def test_pull_hash_skip_returns_no_update(
        self, tmp_path: Path, build_epub: Any, caplog: Any
    ) -> None:
        """pull() with prior whose content_hash matches the downloaded file → upsert=False."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        # First pull to capture the hash
        ctx = FakeContext()
        patch1 = plugin.pull(url, work_dir, None, ctx)

        # Extract the content_hash from the first pull
        content_hash_value = patch1.custom_values["content_hash"].value

        # Create a prior BookView with the stored content_hash (bare-keyed)
        prior = make_book_view(
            book_id=make_book_id(url),
            title="Test Book",
            author="Test Author",
            story_url=url,
            output_filename="Unknown/download.epub",
            num_chapters=1,
            custom_values={
                "content_hash": CustomValueView(
                    value=content_hash_value,
                    value_type="str",
                    updated_at=None,
                ),
            },
        )

        # Clean up work_dir for the second pull
        import shutil

        shutil.rmtree(work_dir)
        work_dir.mkdir()

        # Second pull with prior should skip due to hash match
        with caplog.at_level(logging.INFO):
            patch2 = plugin.pull(url, work_dir, prior, ctx)

        assert patch2.upsert is False
        assert "unchanged after download (content hash match)" in caplog.text

    @responses.activate
    def test_pull_hash_skip_with_prior_no_hash(
        self, tmp_path: Path, build_epub: Any, caplog: Any
    ) -> None:
        """pull() with prior but no stored content_hash → full upsert."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        # Create prior without content_hash
        prior = make_book_view(
            book_id=make_book_id(url),
            title="Test Book",
            author="Test Author",
            story_url=url,
            output_filename="Unknown/download.epub",
            num_chapters=1,
            custom_values={},
        )

        ctx = FakeContext()
        with caplog.at_level(logging.INFO):
            patch = plugin.pull(url, work_dir, prior, ctx)

        # Should still upsert since no hash comparison was possible
        assert patch.upsert is True
        assert "unchanged after download" not in caplog.text


class TestCheckForUpdateDelegates:
    @responses.activate
    def test_check_for_update_delegates_to_helper(self, build_epub: Any) -> None:
        """plugin's check_for_update with a prior carrying etag → delegates to helper,
        returns verdict."""
        url = "https://example.com/book.epub"

        # Mock a HEAD response
        responses.add(
            responses.HEAD,
            url,
            status=200,
            headers={"ETag": '"abc"', "Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        # Create prior with stored etag (bare-keyed)
        prior = make_book_view(
            book_id=make_book_id(url),
            title="Test Book",
            author="Test Author",
            story_url=url,
            output_filename="test.epub",
            num_chapters=1,
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
        from ebookerr_sdk.spi import UpdateCheck

        assert isinstance(result, UpdateCheck)
        assert result.needs_update is False
        assert result.error is None


class TestExtractFilename:
    def test_extract_filename_uses_shared_parser(self) -> None:
        """_extract_filename delegates RFC-5987 parsing to shared parser."""
        # Test RFC-5987 filename*=UTF-8''... (percent-encoded)
        result = _extract_filename(
            "attachment; filename*=UTF-8''Ch%2016.epub", "https://x/y", "https://x/y"
        )
        assert result == "Ch 16.epub"

    def test_a_percent_encoded_url_name_is_decoded(self) -> None:
        """Test that URL path fallback names are percent-decoded."""
        # URL with percent-encoded UTF-8: %20 is space, %C3%A9 is é
        result = _extract_filename("", "https://x/My%20Caf%C3%A9.epub", "https://x/y")
        assert result == "My Café.epub"

    def test_an_encoded_slash_never_makes_a_directory(self) -> None:
        """Test that percent-encoded slashes don't create directory traversal."""
        # %2F is an encoded slash; it should be decoded but then sanitized
        result = _extract_filename("", "https://x/a%2Fb.epub", "https://x/y")
        assert result == "a_b.epub"


class TestSafeComponent:
    def test_safe_component_preserves_spaces(self) -> None:
        """_safe_component preserves spaces in names."""
        assert _safe_component("The Novalist") == "The Novalist"

    def test_safe_component_strips_slash(self) -> None:
        """_safe_component replaces slashes with underscores."""
        assert _safe_component("a/b") == "a_b"

    def test_safe_component_strips_illegal(self) -> None:
        """_safe_component replaces illegal characters with underscores."""
        assert _safe_component('a:b*c?"d') == "a_b_c__d"

    def test_safe_component_empty_returns_book_epub(self) -> None:
        """_safe_component returns 'book.epub' for empty/whitespace-only input."""
        assert _safe_component("   ") == "book.epub"

    def test_safe_component_strips_leading_dots_and_spaces(self) -> None:
        """_safe_component removes leading dots and spaces."""
        assert _safe_component("  .hidden") == "hidden"

    def test_plugin_alias_is_the_shared_function(self) -> None:
        """The plugin's _safe_component import matches the SDK's safe_component."""
        from ebookerr_sdk.download.paths import safe_component

        assert _safe_component is safe_component


class TestBuildFieldsParsesDates:
    """Tests for _build_fields date parsing."""

    def test_build_fields_parses_publication_date(self, tmp_path: Path) -> None:
        """_build_fields with date_published="2026-05-10" yields parsed datetime."""
        from ebookerr_sdk.domain.epub_metadata import EpubMetadata

        plugin = EpubDownloadSourcePlugin()

        meta = EpubMetadata(
            source_url="https://example.com/story",
            title="Test Title",
            author="Test Author",
            language="en",
            date_published="2026-05-10",
            date_updated=None,
            publisher="Test Publisher",
            chapter_count=1,
        )

        fields = plugin._build_fields(
            "https://example.com/download.epub",
            meta,
            "Test Title",
            "Test Author",
            "Test Author/download.epub",
        )

        assert fields["date_published"] == datetime(2026, 5, 10, tzinfo=UTC)

    def test_build_fields_parses_modification_date(self, tmp_path: Path) -> None:
        """_build_fields with date_updated="2026-05-11T08:00:00Z" yields parsed datetime."""
        from ebookerr_sdk.domain.epub_metadata import EpubMetadata

        plugin = EpubDownloadSourcePlugin()

        meta = EpubMetadata(
            source_url="https://example.com/story",
            title="Test Title",
            author="Test Author",
            language="en",
            date_published=None,
            date_updated="2026-05-11T08:00:00Z",
            publisher="Test Publisher",
            chapter_count=1,
        )

        fields = plugin._build_fields(
            "https://example.com/download.epub",
            meta,
            "Test Title",
            "Test Author",
            "Test Author/download.epub",
        )

        assert fields["date_updated"] == datetime(2026, 5, 11, 8, 0, tzinfo=UTC)

    def test_build_fields_omits_unparseable_date(self, tmp_path: Path) -> None:
        """_build_fields with unparseable date_published omits the field."""
        from ebookerr_sdk.domain.epub_metadata import EpubMetadata

        plugin = EpubDownloadSourcePlugin()

        meta = EpubMetadata(
            source_url="https://example.com/story",
            title="Test Title",
            author="Test Author",
            language="en",
            date_published="circa 2026",
            date_updated=None,
            publisher="Test Publisher",
            chapter_count=1,
        )

        fields = plugin._build_fields(
            "https://example.com/download.epub",
            meta,
            "Test Title",
            "Test Author",
            "Test Author/download.epub",
        )

        assert "date_published" not in fields

    def test_build_fields_omits_missing_date(self, tmp_path: Path) -> None:
        """_build_fields with None date_published omits the field."""
        from ebookerr_sdk.domain.epub_metadata import EpubMetadata

        plugin = EpubDownloadSourcePlugin()

        meta = EpubMetadata(
            source_url="https://example.com/story",
            title="Test Title",
            author="Test Author",
            language="en",
            date_published=None,
            date_updated=None,
            publisher="Test Publisher",
            chapter_count=1,
        )

        fields = plugin._build_fields(
            "https://example.com/download.epub",
            meta,
            "Test Title",
            "Test Author",
            "Test Author/download.epub",
        )

        assert "date_published" not in fields

    def test_build_fields_keeps_other_fields(self, tmp_path: Path) -> None:
        """_build_fields preserves title, author, story_url, format, and auto_pull."""
        from ebookerr_sdk.domain.epub_metadata import EpubMetadata

        plugin = EpubDownloadSourcePlugin()

        meta = EpubMetadata(
            source_url="https://example.com/story",
            title="Test Title",
            author="Test Author",
            language="en",
            date_published="2026-05-10",
            date_updated=None,
            publisher="Test Publisher",
            chapter_count=1,
        )

        url = "https://example.com/download.epub"
        output_filename = "Test Author/download.epub"
        fields = plugin._build_fields(url, meta, "Test Title", "Test Author", output_filename)

        assert fields["title"] == "Test Title"
        assert fields["author"] == "Test Author"
        assert fields["story_url"] == url
        assert fields["format"] == "epub"
        assert fields["auto_pull"] == 0


class TestPullPatchDeclaresChapterLink:
    @responses.activate
    def test_pull_patch_declares_single_chapter(self, tmp_path: Path, build_epub: Any) -> None:
        """pull() returns a BookPatch whose chapters == (ChapterLink(...),)."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert len(patch.chapters) == 1
        assert isinstance(patch.chapters[0], ChapterLink)
        assert patch.chapters[0].url == url

    @responses.activate
    def test_pull_patch_chapter_url_matches_story_url(
        self, tmp_path: Path, build_epub: Any
    ) -> None:
        """pull() returns a patch where chapters[0].url == fields['story_url']."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.chapters[0].url == patch.fields["story_url"]

    @responses.activate
    def test_pull_patch_chapter_title_matches_title_field(
        self, tmp_path: Path, build_epub: Any
    ) -> None:
        """pull() returns a patch where chapters[0].title == fields['title']."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()

        url = "https://example.com/download.epub"
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()
        patch = plugin.pull(url, work_dir, None, ctx)

        assert patch.chapters[0].title == patch.fields["title"]


class TestEpubDownloadSourceHeadValidation:
    @pytest.mark.pins("EXP-225")
    @responses.activate
    def test_an_unchanged_epub_is_skipped_by_head(
        self, tmp_path: Path, build_epub: Any, caplog: Any
    ) -> None:
        """Prior with matching etag → HEAD match → upsert=False, no GET, log message."""
        epub_path = build_epub([("Chapter 1", "http://example.com/ch1")])
        epub_bytes = epub_path.read_bytes()
        url = "https://example.com/download.epub"
        etag_value = "abc"

        responses.add(
            responses.HEAD,
            url,
            status=200,
            headers={"ETag": f'"{etag_value}"'},
        )
        responses.add(
            responses.GET,
            url,
            body=epub_bytes,
            status=200,
            headers={"Content-Type": "application/epub+zip"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        # Prior with etag validator (bare-keyed)
        prior = make_book_view(
            book_id=make_book_id(url),
            title="Old Title",
            author="Old Author",
            story_url=url,
            output_filename="old.epub",
            num_chapters=1,
            custom_values={
                "etag": CustomValueView(
                    value=etag_value,
                    value_type="str",
                    updated_at=None,
                ),
            },
        )

        with caplog.at_level(logging.INFO):
            patch = plugin.pull(url, work_dir, prior, ctx)

        assert patch.upsert is False
        assert "unchanged per HEAD validators — skipped without downloading the body" in caplog.text
        # Verify GET was never called
        get_calls = [call for call in responses.calls if call.request.method == "GET"]
        assert len(get_calls) == 0

    @responses.activate
    def test_the_head_skip_carries_forward_a_prior_content_hash(
        self, tmp_path: Path, build_epub: Any
    ) -> None:
        """A prior stored content_hash rides through a HEAD-validated skip patch (EXP-225)."""
        url = "https://example.com/download.epub"
        etag_value = "abc"
        content_hash = hashlib.sha256(b"fake epub bytes").hexdigest()

        responses.add(
            responses.HEAD,
            url,
            status=200,
            headers={"ETag": f'"{etag_value}"'},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        prior = make_book_view(
            book_id=make_book_id(url),
            title="Old Title",
            author="Old Author",
            story_url=url,
            output_filename="old.epub",
            num_chapters=1,
            custom_values={
                "etag": CustomValueView(
                    value=etag_value,
                    value_type="str",
                    updated_at=None,
                ),
                "content_hash": CustomValueView(
                    value=content_hash,
                    value_type="str",
                    updated_at=None,
                ),
            },
        )

        patch = plugin.pull(url, work_dir, prior, ctx)

        assert patch.upsert is False
        assert patch.custom_values["content_hash"].value == content_hash
        assert patch.custom_values["content_hash"].value_type == "str"


class TestEpubDownloadSourceContentTypeMismatch:
    @responses.activate
    def test_a_content_type_mismatch_refuses_to_convert(self, tmp_path: Path) -> None:
        """A server Content-Type that isn't EPUB raises ContentTypeMismatchError (EXP-220)."""
        from ebookerr_sdk.spi import ContentTypeMismatchError

        url = "https://example.com/download.epub"

        responses.add(
            responses.GET,
            url,
            body=b"<html>not an epub</html>",
            status=200,
            headers={"Content-Type": "text/html"},
        )

        plugin = EpubDownloadSourcePlugin(session=requests.Session())

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        ctx = FakeContext()

        with pytest.raises(ContentTypeMismatchError):
            plugin.pull(url, work_dir, None, ctx)
