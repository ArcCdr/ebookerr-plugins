"""TDD tests for EPUB download circuit breaker integration (EXP-269)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.spi import (
    BookView,
    CircuitOpenError,
    SourcePullError,
)
from epub_download_source.plugin import EpubDownloadSourcePlugin


@pytest.fixture
def fake_session() -> MagicMock:
    """Create a fake requests.Session that tracks calls."""
    return MagicMock(spec=requests.Session)


def _make_fake_response(
    status_code: int = 200,
    content_type: str | None = None,
    content: bytes | None = None,
) -> MagicMock:
    """Build a fake response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    if content_type is not None:
        resp.headers["Content-Type"] = content_type
    resp.headers["ETag"] = "test-etag"
    resp.url = "https://example.com/test.epub"

    # Normal streaming
    resp.iter_content = MagicMock(return_value=[content or b"test content"])

    return resp


def _make_book_view(url: str) -> BookView:
    """Build a BookView for testing."""
    return BookView(
        book_id=make_book_id(url),
        title="Test Book",
        author="Test Author",
        story_url=url,
        output_filename="test.epub",
        num_chapters=1,
        status=None,
        rating=None,
        cover_ref=None,
        external=type(
            "External",
            (),
            {
                "provider": None,
                "library_id": None,
                "item_id": None,
                "collection_id": None,
                "item_url": None,
            },
        )(),  # type: ignore
        progress=type(
            "Progress",
            (),
            {
                "percent": None,
                "completed": False,
                "position": None,
                "total": None,
                "locator": None,
            },
        )(),  # type: ignore
        custom_values={},
        chapters=None,
        read_position=None,
        overridden_fields=set(),
        file_size=None,
    )


class MockContextManager:
    """A context manager that tracks failures for circuit breaker simulation."""

    def __init__(self, key: str, guard: MockCircuitGuard):
        """Initialize the context manager."""
        self.key = key
        self.guard = guard

    def __enter__(self):
        """Enter the context."""
        return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and track failures."""
        if exc_type is not None:
            # Only track RequestException failures, not CircuitOpenError
            if issubclass(exc_type, requests.RequestException):
                self.guard.failure_counts[self.key] = self.guard.failure_counts.get(self.key, 0) + 1
                if self.guard.failure_counts[self.key] >= 3:
                    self.guard.open_keys.add(self.key)
        else:
            # Reset on success
            self.guard.failure_counts[self.key] = 0
        return False  # Don't suppress exceptions


class MockCircuitGuard:
    """Mock CircuitGuard that tracks state and call counts per key."""

    def __init__(self):
        """Initialize the mock circuit guard."""
        self.call_counts = {}
        self.open_keys = set()
        self.failure_counts = {}

    def guard(self, key: str, *, label: str | None = None) -> MockContextManager:
        """Return a context manager that tracks calls and can simulate open state."""
        if key not in self.call_counts:
            self.call_counts[key] = 0
            self.failure_counts[key] = 0

        self.call_counts[key] += 1

        # If this key is open, raise CircuitOpenError
        if key in self.open_keys:
            raise CircuitOpenError(key=key, label=label or key, retry_at=datetime.now(UTC))

        # Return a context manager that records failures
        return MockContextManager(key, self)

    def is_open(self, key: str) -> bool:
        """Whether the breaker for key is currently refusing calls."""
        return key in self.open_keys


class MockPluginContext:
    """Mock PluginContext with optional circuit attribute."""

    def __init__(self, circuit: MockCircuitGuard | None = None):
        """Initialize the mock context."""
        self.circuit = circuit

    def report(self, progress: float) -> None:
        """Report progress."""
        pass

    def check_cancelled(self) -> None:
        """Check for cancellation."""
        pass

    def auth_headers(self, url: str) -> dict[str, str]:
        """Return auth headers for URL."""
        return {}


@pytest.mark.pins("EXP-269")
def test_an_epub_host_that_is_unreachable_is_fetched_once_not_once_per_book(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that a dead host only gets 3 GET calls before circuit opens."""
    guard = MockCircuitGuard()
    ctx = MockPluginContext(circuit=guard)

    # GET raises ConnectionError
    fake_session.get.side_effect = requests.ConnectionError("Connection refused")

    plugin = EpubDownloadSourcePlugin(session=fake_session)
    url_base = "https://a.example.com/books"

    # Download 20 URLs on one host
    for i in range(20):
        url = f"{url_base}/{i}.epub"
        with pytest.raises(SourcePullError) as exc_info:
            plugin.pull(url, tmp_path, None, ctx)
        # After 3 failures, should see "is not reachable; retrying automatically"
        if i >= 3:
            assert "is not reachable; retrying automatically" in str(exc_info.value)

    # Verify GET was called exactly 3 times (the other 17 were blocked by open breaker)
    assert fake_session.get.call_count == 3


@pytest.mark.pins("EXP-269")
def test_an_open_breaker_makes_no_request(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that an open breaker prevents GET request and returns SourcePullError."""
    guard = MockCircuitGuard()
    guard.open_keys.add("host:a.example.com")
    ctx = MockPluginContext(circuit=guard)

    plugin = EpubDownloadSourcePlugin(session=fake_session)
    url = "https://a.example.com/book.epub"

    with pytest.raises(SourcePullError) as exc_info:
        plugin.pull(url, tmp_path, None, ctx)

    # Should mention the host
    assert "a.example.com" in str(exc_info.value)

    # GET should never have been called
    fake_session.get.assert_not_called()


@pytest.mark.pins("EXP-269")
def test_a_404_never_trips_the_breaker(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that 404 response does not open circuit."""
    guard = MockCircuitGuard()
    ctx = MockPluginContext(circuit=guard)

    fake_session.get.return_value = _make_fake_response(status_code=404)

    plugin = EpubDownloadSourcePlugin(session=fake_session)

    # Call 10 times with 404s
    for i in range(10):
        url = f"https://a.example.com/book{i}.epub"
        with pytest.raises(SourcePullError) as exc_info:
            plugin.pull(url, tmp_path, None, ctx)
        assert "HTTP 404" in str(exc_info.value)

    # Breaker should still be closed
    assert not guard.is_open("host:a.example.com")
    assert fake_session.get.call_count == 10


@pytest.mark.pins("EXP-269")
def test_a_timeout_trips_the_breaker(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that timeout opens circuit after 3 attempts."""
    guard = MockCircuitGuard()
    ctx = MockPluginContext(circuit=guard)

    fake_session.get.side_effect = requests.ReadTimeout("Request timeout")

    plugin = EpubDownloadSourcePlugin(session=fake_session)

    # First 3 calls should attempt and fail
    for i in range(3):
        url = f"https://a.example.com/book{i}.epub"
        with pytest.raises(SourcePullError):
            plugin.pull(url, tmp_path, None, ctx)

    # Fourth call should hit the open breaker and return fail-closed
    assert guard.is_open("host:a.example.com")
    url = "https://a.example.com/book3.epub"
    with pytest.raises(SourcePullError) as exc_info:
        plugin.pull(url, tmp_path, None, ctx)
    assert "is not reachable; retrying automatically" in str(exc_info.value)


@pytest.mark.pins("EXP-269")
def test_a_malformed_epub_never_trips_the_breaker(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that malformed EPUB never trips breaker."""
    guard = MockCircuitGuard()
    ctx = MockPluginContext(circuit=guard)

    # 200 response but body is not valid EPUB
    fake_session.get.return_value = _make_fake_response(
        status_code=200,
        content_type="application/epub+zip",
        content=b"not an epub",
    )

    plugin = EpubDownloadSourcePlugin(session=fake_session)

    # Call 10 times with malformed EPUBs
    for i in range(10):
        url = f"https://a.example.com/book{i}.epub"
        with pytest.raises(SourcePullError) as exc_info:
            plugin.pull(url, tmp_path, None, ctx)
        assert "not a valid EPUB" in str(exc_info.value)

    # Breaker should still be closed (malformed happens after GET returns 200)
    assert not guard.is_open("host:a.example.com")
    assert fake_session.get.call_count == 10


@pytest.mark.pins("EXP-269")
def test_a_successful_pull_resets_the_count(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that successful 200 response resets failure count."""
    guard = MockCircuitGuard()
    ctx = MockPluginContext(circuit=guard)

    plugin = EpubDownloadSourcePlugin(session=fake_session)

    # Fail, fail, success, fail
    fake_session.get.side_effect = [
        requests.ConnectionError("fail1"),
        requests.ConnectionError("fail2"),
        _make_fake_response(
            status_code=200,
            content_type="application/epub+zip",
        ),
        requests.ConnectionError("fail3"),
    ]

    from contextlib import ExitStack

    for i in range(4):
        url = f"https://a.example.com/book{i}.epub"
        try:
            with ExitStack() as stack:
                fmt_mismatch = "ebookerr_sdk.download.routing.response_format_mismatch"
                stack.enter_context(patch(fmt_mismatch, return_value=None))
                stack.enter_context(patch.object(Path, "read_bytes", return_value=b"content"))
                stack.enter_context(patch.object(Path, "rename"))
                stack.enter_context(patch.object(Path, "mkdir"))
                stack.enter_context(
                    patch(
                        "epub_download_source.plugin.EpubDownloadSourcePlugin._read_epub_metadata",
                        return_value=MagicMock(author="Test Author", title="Test Title"),
                    )
                )
                stack.enter_context(
                    patch("epub_download_source.plugin.EpubDownloadSourcePlugin._download_to_file")
                )
                stack.enter_context(patch("epub_download_source.plugin.epub_content_hash"))
                plugin.pull(url, tmp_path, None, ctx)
        except (SourcePullError, OSError):
            pass

    # After success, failure count should be reset, so 4th failure doesn't open
    assert not guard.is_open("host:a.example.com")


@pytest.mark.pins("EXP-269")
def test_the_breaker_is_shared_with_the_document_sources(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that EPUB Source and document sources share the same breaker per host."""
    guard = MockCircuitGuard()
    ctx = MockPluginContext(circuit=guard)

    # First, open the breaker through the EPUB Source
    fake_session.get.side_effect = requests.ConnectionError("Connection refused")
    plugin = EpubDownloadSourcePlugin(session=fake_session)

    # Fail 3 times to open the breaker
    for i in range(3):
        url = f"https://a.example.com/book{i}.epub"
        with pytest.raises(SourcePullError):
            plugin.pull(url, tmp_path, None, ctx)

    # Breaker should be open
    assert guard.is_open("host:a.example.com")

    # Now test that document_download also respects the same breaker
    from ebookerr_sdk.download.document import download_convert_stage

    url = "https://a.example.com/test.docx"
    with pytest.raises(SourcePullError) as exc_info:
        download_convert_stage(
            url,
            tmp_path,
            None,
            ctx,
            session=fake_session,
            auth_headers={},
            timeout_s=10.0,
            namespace="docx_download_source",
            source_suffix=".docx",
            fmt="docx",
            convert=lambda src, epub: MagicMock(),
            log_label="DOCX",
        )

    # Should be refused by open breaker, not by a GET attempt
    assert "is not reachable right now; try again later" in str(exc_info.value)
    # GET count should still be 3 from EPUB source only
    assert fake_session.get.call_count == 3


@pytest.mark.pins("EXP-269")
def test_the_pull_is_unguarded_without_a_circuit(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that circuit=None runs 20 requests unguarded."""
    ctx = MockPluginContext(circuit=None)
    fake_session.get.side_effect = requests.ConnectionError("fail")

    plugin = EpubDownloadSourcePlugin(session=fake_session)

    # Call 20 times with no circuit
    for i in range(20):
        url = f"https://a.example.com/book{i}.epub"
        with pytest.raises(SourcePullError):
            plugin.pull(url, tmp_path, None, ctx)

    # All 20 should have been attempted
    assert fake_session.get.call_count == 20


@pytest.mark.pins("EXP-269")
def test_two_hosts_keep_two_breakers(
    tmp_path: Path,
    fake_session: MagicMock,
) -> None:
    """Test that dead host a.example doesn't affect b.example."""
    guard = MockCircuitGuard()
    ctx = MockPluginContext(circuit=guard)

    plugin = EpubDownloadSourcePlugin(session=fake_session)

    # a.example.com fails 3 times and opens
    fake_session.get.side_effect = [
        requests.ConnectionError("fail"),
        requests.ConnectionError("fail"),
        requests.ConnectionError("fail"),
    ]

    for i in range(3):
        url = f"https://a.example.com/book{i}.epub"
        with pytest.raises(SourcePullError):
            plugin.pull(url, tmp_path, None, ctx)

    assert guard.is_open("host:a.example.com")

    # Now b.example.com succeeds
    fake_session.get.side_effect = None
    fake_session.get.return_value = _make_fake_response(
        status_code=200,
        content_type="application/epub+zip",
    )

    from contextlib import ExitStack

    url = "https://b.example.com/book.epub"
    with ExitStack() as stack:
        fmt_mismatch = "ebookerr_sdk.download.routing.response_format_mismatch"
        stack.enter_context(patch(fmt_mismatch, return_value=None))
        stack.enter_context(patch.object(Path, "read_bytes", return_value=b"content"))
        stack.enter_context(patch.object(Path, "rename"))
        stack.enter_context(patch.object(Path, "mkdir"))
        stack.enter_context(
            patch(
                "epub_download_source.plugin.EpubDownloadSourcePlugin._read_epub_metadata",
                return_value=MagicMock(author="Test Author", title="Test Title"),
            )
        )
        stack.enter_context(
            patch("epub_download_source.plugin.EpubDownloadSourcePlugin._download_to_file")
        )
        stack.enter_context(patch("epub_download_source.plugin.epub_content_hash"))
        plugin.pull(url, tmp_path, None, ctx)

    # b.example.com should not be open
    assert not guard.is_open("host:b.example.com")
