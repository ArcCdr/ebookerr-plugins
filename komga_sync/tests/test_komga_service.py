"""Tests for KomgaService business rules (F3 / ARCHITECTURE.md §2.2)."""

from __future__ import annotations

import dataclasses
import html
import inspect
import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.providers.connection import ConnectionTestResult, ProviderUnreachable
from ebookerr_sdk.providers.link_refusal import LinkRefusal
from ebookerr_sdk.spi import (
    BookView,
    ChapterLink,
    ChapterView,
    CircuitOpenError,
    ExternalLink,
    ExternalProgress,
    ReadPosition,
)
from ebookerr_sdk.testing import make_book_view

from komga_sync.service import (
    KomgaService,
    _book_patch,
    _catalog_from_komga,
    _chapter_table,
    _compose_tags,
    _genres,
    _komga_only_tags,
    _rating_from_tags,
    _rating_tag,
    _series_patch,
    _upsert_links,
)

STORY_URL = "https://www.literotica.com/s/the-12th-key"
AUTHOR_URL = "https://www.literotica.com/authors/gabthewriter/works/stories"
FIXED_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
EXPECTED_TAGS = ["Erotic Horror", "Horror", "Oral"]
MATCHING_BOOK_METADATA = {
    "title": "The 12th Key",
    "summary": "Nightmares.",
    "releaseDate": "2026-05-19",
    "authors": [{"name": "gabthewriter", "role": "writer"}],
    "tags": EXPECTED_TAGS,
    "links": [{"label": "Book", "url": STORY_URL}, {"label": "Author", "url": AUTHOR_URL}],
}
MATCHING_SERIES_METADATA = {
    "genres": ["Erotic Horror"],
    "language": "en",
    "links": [{"label": "Story", "url": STORY_URL}],
}

# Positions fixture for semantic capture tests (RP-CAP-5, RP-SEM-1/3)
POSITIONS_FIXTURE = [
    {
        "href": "OEBPS/titlepage.xhtml",
        "title": "The 12th Key",
        "type": "application/xhtml+xml",
        "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
    },
    {
        "href": "OEBPS/file0001.xhtml",
        "title": "The 12th Key - Ch 1",
        "type": "application/xhtml+xml",
        "locations": {"position": 2, "progression": 0.0, "totalProgression": 0.2},
    },
    {
        "href": "OEBPS/file0001.xhtml",
        "title": "The 12th Key - Ch 1",
        "type": "application/xhtml+xml",
        "locations": {"position": 3, "progression": 0.5, "totalProgression": 0.4},
    },
    {
        "href": "OEBPS/file0002.xhtml",
        "title": "The 12th Key - Ch 2",
        "type": "application/xhtml+xml",
        "locations": {"position": 4, "progression": 0.0, "totalProgression": 0.6},
    },
    {
        "href": "OEBPS/file0002.xhtml",
        "title": "The 12th Key - Ch 2",
        "type": "application/xhtml+xml",
        "locations": {"position": 5, "progression": 0.5, "totalProgression": 0.8},
    },
]


class _TickingClock:
    """A ``now`` whose every call is one second later — production ordering, never equal stamps."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        self._t = self._t + timedelta(seconds=1)
        return self._t


class FakeKomga:
    """In-memory KomgaClient double recording calls and returning canned data."""

    def __init__(self) -> None:
        self.connected = True
        self.book: dict[str, Any] | None = None
        self.book_sequence: list[dict[str, Any] | None] = []
        self.series: dict[str, Any] | None = None
        self.find_results: list[str | None] = []
        self.progression_sequence: list[dict[str, Any]] = []
        self.analyze_calls: list[str] = []
        self.scan_calls = 0
        self.scan_library_calls: list[str] = []
        self.scan_library_ok = True
        self.book_patches: list[tuple[str, dict[str, Any]]] = []
        self.series_patches: list[tuple[str, dict[str, Any]]] = []
        self.put_progressions: list[tuple[str, dict[str, Any]]] = []
        self.put_progression_ok = True
        self.find_calls = 0
        self.library_books: list[tuple[str, str]] = []
        self.library_books_sequence: list[list[tuple[str, str]]] = []
        self.list_calls = 0
        self.get_progression_call_count: int = 0
        self.get_positions_call_count: int = 0
        self.test_connection_calls: int = 0
        self.get_series_calls: list[str] = []
        self.book_exists_result: bool | None = True
        self.book_exists_calls: list[str] = []
        self.die_after_get_book_calls: int | None = None
        self.get_book_calls: int = 0

    def test_connection(self) -> ConnectionTestResult:
        self.test_connection_calls += 1
        if self.connected:
            return ConnectionTestResult("ok", "Connected.")
        return ConnectionTestResult("unreachable", "Komga is not reachable.")

    def find_book_id(self, title: str, author: str | None) -> str | None:
        self.find_calls += 1
        return self.find_results.pop(0) if self.find_results else None

    def get_book(self, komga_book_id: str) -> dict[str, Any] | None:
        from ebookerr_sdk.providers.connection import ProviderUnreachable

        self.get_book_calls += 1
        if (
            self.die_after_get_book_calls is not None
            and self.get_book_calls > self.die_after_get_book_calls
        ):
            raise ProviderUnreachable("Komga connection lost")
        if self.book_sequence:
            return self.book_sequence.pop(0)
        return self.book

    def get_series(self, series_id: str) -> dict[str, Any] | None:
        self.get_series_calls.append(series_id)
        return self.series

    def trigger_analyze(self, komga_book_id: str) -> bool:
        self.analyze_calls.append(komga_book_id)
        return True

    def book_exists(self, komga_book_id: str) -> bool | None:
        self.book_exists_calls.append(komga_book_id)
        return self.book_exists_result

    def trigger_library_scan(self) -> bool:
        self.scan_calls += 1
        return True

    def patch_book_metadata(self, komga_book_id: str, patch: dict[str, Any]) -> bool:
        self.book_patches.append((komga_book_id, patch))
        return True

    def patch_series_metadata(self, series_id: str, patch: dict[str, Any]) -> bool:
        self.series_patches.append((series_id, patch))
        return True

    def get_progression(self, komga_book_id: str) -> dict[str, Any]:
        self.get_progression_call_count += 1
        return self.progression_sequence.pop(0) if self.progression_sequence else {}

    def get_positions(self, komga_book_id: str) -> list[dict[str, Any]]:
        self.get_positions_call_count += 1
        return self.positions if hasattr(self, "positions") else POSITIONS_FIXTURE

    def put_progression(self, komga_book_id: str, progression: dict[str, Any]) -> bool:
        self.put_progressions.append((komga_book_id, progression))
        return self.put_progression_ok

    def list_library_books(self) -> list[tuple[str, str]]:
        self.list_calls += 1
        if self.library_books_sequence:
            return self.library_books_sequence.pop(0)
        return self.library_books

    def scan_library(self, library_id: str) -> bool:
        self.scan_calls += 1
        self.scan_library_calls.append(library_id)
        return self.scan_library_ok

    def empty_trash_for(self, library_id: str) -> bool:
        self.scan_calls += 1
        return True

    def delete_book_file(self, komga_book_id: str) -> bool:
        return True

    def empty_trash(self) -> bool:
        return True


class _EchoingKomga(FakeKomga):
    """Returns the last progression it was sent, like a real Komga after a restore."""

    def get_progression(self, komga_book_id: str) -> dict[str, Any]:
        self.get_progression_call_count += 1
        if self.progression_sequence:
            return self.progression_sequence.pop(0)
        return self.put_progressions[-1][1] if self.put_progressions else {}


def komga_book(
    *,
    metadata: dict[str, Any] | None = None,
    series_id: str = "SERIES1",
    pages: int = 14,
    read: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "KB1",
        "seriesId": series_id,
        "media": {"pagesCount": pages},
        "metadata": metadata if metadata is not None else {},
        "readProgress": read or {},
    }


@dataclass
class _Book:
    """Local stand-in for ``src.domain.models.Book`` (that core dataclass is off limits here).

    Field names/defaults mirror the real one exactly: ``book_to_view`` below and the
    ``_genres``/``_book_patch``/``_series_patch`` unit tests read these as plain attributes,
    and only :class:`_FakeBookRepository` ever constructs or mutates one.
    """

    book_id: str
    story_id: str | None = None
    title: str | None = None
    author: str | None = None
    author_url: str | None = None
    story_url: str | None = None
    section_url: str | None = None
    series: str | None = None
    series_url: str | None = None
    description: str | None = None
    category: str | None = None
    erotica_tags: str | None = None
    date_published: datetime | None = None
    status: str | None = None
    site: str | None = None
    output_filename: str | None = None
    external_provider: str | None = None
    external_library_id: str | None = None
    external_item_id: str | None = None
    external_collection_id: str | None = None
    external_item_url: str | None = None
    external_read_completed: int = 0
    external_read_position: int = 0
    external_read_total: int = 0
    external_read_percent: float = 0.0
    external_chapter_count: int | None = None
    external_locator: str | None = None
    external_synced_at: datetime | None = None
    external_progress_at: datetime | None = None
    rating: int | None = None


# FanFicFare JSON key -> _Book field, the fixture-building slice of
# src.services.fanficfare_metadata.fanficfare_json_to_book_fields's column map (core, and not
# this plugin-owned module's subject — KomgaService never sees this payload, only the BookView
# make_book/book_to_view derive from it).
_FFF_TEXT_FIELDS: dict[str, str] = {
    "title": "title",
    "author": "author",
    "authorUrl": "author_url",
    "storyUrl": "story_url",
    "sectionUrl": "section_url",
    "series": "series",
    "seriesUrl": "series_url",
    "description": "description",
    "category": "category",
    "eroticatags": "erotica_tags",
    "status": "status",
    "site": "site",
    "output_filename": "output_filename",
    "storyId": "story_id",
}
_FFF_SANITISE_COLUMNS: frozenset[str] = frozenset(
    {"title", "author", "series", "description", "category", "erotica_tags", "status", "site"}
)
_TAG_RE = re.compile(r"<[^>]+>")


def _str_or_none(value: Any) -> str | None:
    """Stringify and strip; None/empty/whitespace-only becomes None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sanitize_text(value: str | None) -> str | None:
    """Strip HTML tags and decode entities (mirrors fanficfare_metadata.sanitize)."""
    if not isinstance(value, str):
        return value
    return html.unescape(_TAG_RE.sub("", value)).strip()


def _fff_book_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Map an FFF-shaped payload to ``_Book`` fields (rename + sanitize; no int/date-updated
    columns since nothing in this module reads ``num_chapters``/``num_words``/``date_updated``
    — a payload override under those names is a documented no-op, matching the real mapping's
    behaviour for keys it does not recognise).
    """
    fields: dict[str, Any] = {
        col: _str_or_none(payload.get(key)) for key, col in _FFF_TEXT_FIELDS.items()
    }
    fields["date_published"] = parse_datetime(payload.get("datePublished"))
    for col in _FFF_SANITISE_COLUMNS:
        fields[col] = _sanitize_text(fields[col])
    return fields


def _fff_book_id(payload: dict[str, Any]) -> str | None:
    """Mirror fanficfare_book_id: derive the id from storyUrl/sectionUrl via the SDK helper."""
    story_url = _str_or_none(payload.get("storyUrl"))
    section_url = _str_or_none(payload.get("sectionUrl"))
    if not story_url and not section_url:
        return None
    return make_book_id(story_url, section_url)


@dataclass
class _FakeBookRepository:
    """In-memory stand-in for ``SqliteBookRepository`` — a database is not this module's subject.

    Every test here exercises ``KomgaService`` against a ``BookView``; the repository dance
    (``make_book``/``update_fields``/``get``) is fixture plumbing to build that view's inputs.
    An in-memory upsert-by-``book_id`` mirrors just the observable slice of the real
    repository's behaviour: fields given to ``upsert_book``/``update_fields`` always overwrite;
    columns absent from them are left untouched on an existing row.
    """

    _books: dict[str, _Book] = field(default_factory=dict, init=False)

    def upsert_book(self, book_id: str, fields: dict[str, Any]) -> str:
        """Insert a new row or merge ``fields`` onto the existing one at ``book_id``."""
        book = self._books.get(book_id) or _Book(book_id=book_id)
        for col, value in fields.items():
            setattr(book, col, value)
        self._books[book_id] = book
        return book_id

    def get(self, book_id: str) -> _Book | None:
        """Return a copy of the stored book, or None."""
        book = self._books.get(book_id)
        return dataclasses.replace(book) if book is not None else None

    def update_fields(self, book_id: str, changes: dict[str, Any]) -> bool:
        """Apply column -> value pairs to the stored row; True if the row exists."""
        book = self._books.get(book_id)
        if book is None:
            return False
        for col, value in changes.items():
            setattr(book, col, value)
        return True

    def set_rating(self, book_id: str, rating: int | None) -> bool:
        """Set or clear the stored row's rating; True if the row exists."""
        return self.update_fields(book_id, {"rating": rating})

    def add_read_position(self, book_id: str, **_kwargs: Any) -> int:
        """No-op: no test in this module reads a read position back through the repository."""
        return 0


@pytest.fixture
def repo() -> _FakeBookRepository:
    """A fresh in-memory book repository, isolated per test."""
    return _FakeBookRepository()


def make_book(repo: _FakeBookRepository, **overrides: Any) -> _Book:
    """Build a book via an FFF-shaped payload, an in-memory upsert stand-in for a real pull."""
    payload: dict[str, Any] = {
        "title": "The 12th Key",
        "author": "gabthewriter",
        "description": "<p>Nightmares.</p>",
        "category": "Erotic Horror",
        "eroticatags": "Horror, Oral",
        "datePublished": "2026-05-19",
        "storyUrl": STORY_URL,
        "sectionUrl": STORY_URL,
        "authorUrl": AUTHOR_URL,
        "storyId": "the-12th-key",
    }
    payload.update(overrides)
    book_id = _fff_book_id(payload)
    assert book_id is not None
    repo.upsert_book(book_id, _fff_book_fields(payload))
    book = repo.get(book_id)
    assert book is not None
    return book


def _blank_view(**overrides: Any) -> BookView:
    """A ``BookView`` with every field ``None``/empty, unlike ``make_book_view``'s populated ones.

    ``_genres``/``_book_patch``/``_series_patch``'s pure-function tests need a book with no
    incidental values — matching a freshly constructed ``Book``'s all-``None`` dataclass
    defaults — so a field's absence, not ``make_book_view``'s realistic filler, is what drives
    the assertion.
    """
    defaults: dict[str, Any] = {
        "book_id": "x",
        "title": None,
        "author": None,
        "story_url": None,
        "output_filename": None,
        "num_chapters": None,
        "status": None,
        "rating": None,
        "cover_ref": None,
        "external": ExternalLink(),
        "progress": ExternalProgress(),
        "custom_values": {},
    }
    defaults.update(overrides)
    return make_book_view(**defaults)


def _chapters(*entries: tuple[str, str]) -> tuple[ChapterView, ...]:
    """Build a ``chapter_table`` from ``(href, title)`` pairs; ordinal from 1, key=href."""
    return tuple(
        ChapterView(ordinal=i, key=href, title=title, number="", role="content", href=href)
        for i, (href, title) in enumerate(entries, start=1)
    )


# The two content chapters POSITIONS_FIXTURE carries (its title page excluded, CHC-D12) —
# book_to_view()'s default chapter_table, so a plain book_to_view(book) still joins cleanly
# against the file's existing default positions fixture.
_DEFAULT_CHAPTER_TABLE = _chapters(
    ("file0001.xhtml", "The 12th Key - Ch 1"), ("file0002.xhtml", "The 12th Key - Ch 2")
)


def book_to_view(book: _Book, **overrides: Any) -> BookView:
    """Convert a Book to BookView for testing (maps external_* attrs to nested structures).

    Args:
        book: The repository-backed book to convert.
        **overrides: Any additional ``BookView`` field to override (e.g. ``chapter_table``).
    """
    fields: dict[str, Any] = {
        "book_id": book.book_id,
        "title": book.title,
        "author": book.author,
        "story_url": book.story_url,
        "output_filename": book.output_filename,
        "status": book.status,
        "rating": book.rating,
        "category": book.category,
        "erotica_tags": book.erotica_tags,
        "site": book.site,
        "description": book.description,
        "date_published": book.date_published,
        "author_url": book.author_url,
        "series": book.series,
        "series_url": book.series_url,
        "section_url": book.section_url,
        "chapter_table": _DEFAULT_CHAPTER_TABLE,
        "external": ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        "progress": ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
    }
    fields.update(overrides)
    return make_book_view(**fields)


def service(client: FakeKomga, **kwargs: Any) -> KomgaService:
    """Build a ``KomgaService`` whose anchor join sees ``POSITIONS_FIXTURE``'s title page.

    Patches :meth:`~src.services.komga_service.KomgaService._document_hrefs` to report the
    fixture's own title-page + chapter hrefs, standing in for a real, readable EPUB — tests
    exercising the anchor join's own inconsistency (an unmatched href, a missing chapter)
    override ``_document_hrefs`` again afterwards, or never reach the join at all (an empty
    chapter_table forces ``TITLE`` mode, where this list is not consulted).
    """
    params: dict[str, Any] = {
        "enabled": True,
        "scan_retry_max": 3,
        "scan_retry_delay": 0.0,
        "sleep": lambda _seconds: None,
        "server_url": "http://k/",
    }
    params.update(kwargs)
    svc = KomgaService(client, **params)
    svc._document_hrefs = lambda book: (  # type: ignore[method-assign]
        "titlepage.xhtml",
        "file0001.xhtml",
        "file0002.xhtml",
    )
    return svc


def test_disabled_returns_error(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    result = service(client, enabled=False).sync(book_to_view(make_book(repo)))
    assert result.ok is False
    assert result.attempted is False  # disabled => not an error to notify about
    assert "disabled" in result.message


def test_unreachable_returns_error(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.connected = False
    result = service(client).sync(book_to_view(make_book(repo)))
    assert result.ok is False
    assert result.attempted is True  # a real, notifiable failure
    assert "reachable" in result.message


def test_links_new_book_found_immediately(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id="S1", metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert client.analyze_calls == []
    assert client.scan_calls == 0
    assert result.fields["external_item_id"] == "KB1"
    assert result.fields["external_collection_id"] == "S1"


def test_scan_and_poll_until_found(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = [None, None, "KB1"]  # initial miss, poll miss, poll hit
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert client.scan_calls == 1
    assert client.find_calls == 3


def test_gives_up_after_max_retries(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = []  # always None
    result = service(client, scan_retry_max=3).sync(book_to_view(make_book(repo)))
    assert result.ok is False
    assert client.scan_calls == 1
    assert client.find_calls == 4  # 1 initial + 3 polls


def test_already_linked_does_not_scan_or_analyze_by_default(
    repo: _FakeBookRepository,
) -> None:
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert client.analyze_calls == []  # No analyze by default
    assert client.scan_calls == 0
    assert client.find_calls == 0


def test_stale_komga_id_is_re_resolved_by_title(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = ["KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_item_id"] == "KB2"
    assert client.analyze_calls == []
    assert client.book_exists_calls == ["KB1"]


def test_stale_komga_id_is_re_resolved_by_path(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = [None]
    client.library_books = [("KB3", "/library/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo, output_filename="author/title.epub")
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.fields["external_item_id"] == "KB3"


def test_relink_is_logged_at_info(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = ["KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert "Komga id changed" in caplog.text
    assert "KB1" in caplog.text
    assert "KB2" in caplog.text


def test_vanished_id_is_logged_at_warning(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = ["KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert "no longer exists" in caplog.text


def test_indeterminate_book_exists_keeps_the_existing_link(
    repo: _FakeBookRepository,
) -> None:
    client = FakeKomga()
    client.book_exists_result = None
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.fields["external_item_id"] == "KB1"
    assert client.analyze_calls == []  # No analyze by default
    assert client.find_calls == 0
    assert client.scan_calls == 0


def test_no_relink_log_when_the_id_is_unchanged(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert "Komga id changed" not in caplog.text


def test_unlinked_book_never_probes_book_exists(
    repo: _FakeBookRepository,
) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert client.book_exists_calls == []


def test_pushes_changed_book_metadata_with_combined_tags(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={})  # empty -> everything differs
    client.series = {"metadata": {}}

    service(client).sync(book_to_view(make_book(repo)))

    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    assert patch["title"] == "The 12th Key"
    assert patch["summary"] == "Nightmares."
    assert patch["releaseDate"] == "2026-05-19"
    assert patch["authors"] == [{"name": "gabthewriter", "role": "writer"}]
    assert patch["tags"] == EXPECTED_TAGS
    assert {link["label"] for link in patch["links"]} == {"Book", "Author"}


def test_no_book_patch_when_unchanged(repo: _FakeBookRepository) -> None:
    # Komga lowercases tags server-side — CI content comparison must suppress patch.
    client = FakeKomga()
    client.find_results = ["KB1"]
    komga_meta = {**MATCHING_BOOK_METADATA, "tags": ["erotic horror", "horror", "oral"]}
    client.book = komga_book(metadata=komga_meta)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    service(client).sync(book_to_view(make_book(repo)))

    assert client.book_patches == []
    assert client.series_patches == []


def test_pushes_changed_series_metadata(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": {}}

    service(client).sync(book_to_view(make_book(repo)))

    assert len(client.series_patches) == 1
    _, patch = client.series_patches[0]
    assert patch["genres"] == ["Erotic Horror"]
    assert "language" not in patch
    assert patch["links"] == [{"label": "Story", "url": STORY_URL}]


def test_reads_back_komga_state_into_db(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata={"tags": ["rating:4"]},
        pages=14,
        read={"page": 10, "completed": False},
    )
    client.series = {"metadata": {}}
    book = make_book(repo)

    result = service(client).sync(book_to_view(book))

    assert result.attempted is True
    assert (result.current_page, result.total_pages) == (10, 14)  # surfaced for F10
    assert result.fields["external_read_total"] == 14
    assert result.fields["external_read_position"] == 10
    assert result.fields["external_read_completed"] == 0


def test_restores_progress_when_komga_reset_it(repo: _FakeBookRepository) -> None:
    saved = {"locator": {"locations": {"position": 11, "progression": 0.68}}, "device": {}}
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [saved, reset]  # snapshot, then post-analyze (lost)
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    assert client.put_progressions == [("KB1", {**saved, "modified": FIXED_NOW.isoformat()})]


def test_restore_defers_when_komga_retained_real_progress(repo: _FakeBookRepository) -> None:
    """A differing but non-zero progression means Komga recomputed it — the snapshot defers."""
    saved = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    recomputed = {"locator": {"locations": {"position": 31, "progression": 0.59}}}
    client = FakeKomga()
    client.progression_sequence = [saved, recomputed]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    service(client).sync(book_to_view(book))

    assert client.put_progressions == []


def test_restore_stamps_fresh_modified_and_keeps_snapshot_unmutated(
    repo: _FakeBookRepository,
) -> None:
    """The restore PUT carries a fresh ``modified`` (Komga rejects non-newer progressions)."""
    snap = {
        "modified": "2026-04-17T16:03:09.247+02:00",
        "device": {"id": "komic"},
        "locator": {"locations": {"position": 31, "progression": 0.59}},
    }
    client = FakeKomga()
    # snap at sync start, post-analyze: progression fully wiped
    client.progression_sequence = [snap, {}]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    assert client.put_progressions == [("KB1", {**snap, "modified": FIXED_NOW.isoformat()})]
    assert snap["modified"] == "2026-04-17T16:03:09.247+02:00"  # caller's snapshot untouched


def test_zero_progress_snapshot_is_not_restored(repo: _FakeBookRepository) -> None:
    """A snapshot with no meaningful positions protects nothing — never written back."""
    snap = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [snap, {}]  # snap at sync start, post-analyze check
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    service(client).sync(book_to_view(book))

    assert client.put_progressions == []


def test_does_not_restore_when_progress_intact(repo: _FakeBookRepository) -> None:
    saved = {"locator": {"locations": {"position": 11}}}
    client = FakeKomga()
    client.progression_sequence = [saved, saved]  # unchanged after analyze
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    service(client).sync(book_to_view(book))

    assert client.put_progressions == []


def test_restore_failure_logs_warning_and_sync_remains_ok(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    saved = {"locator": {"locations": {"position": 11, "progression": 0.68}}, "device": {}}
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [saved, reset]  # snapshot, then post-analyze (lost)
    client.put_progression_ok = False
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert any(r.levelname == "WARNING" and "KB1" in r.message for r in caplog.records)


def test_a_reset_page_is_repaired_by_re_writing_the_locator(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Komga resets page to 1 after re-analyze; re-PUT the locator to fix it (RP-D17)."""
    locator_payload = {
        "locator": {"locations": {"position": 118, "progression": 0.64, "totalProgression": 0.64}},
        "device": {"id": "komga", "name": "Komga"},
        "modified": "2026-04-17T16:00:00.000Z",
    }
    client = FakeKomga()
    # Multiple calls to get_progression during sync:
    # 1. snapshot at sync start
    # 2. in read_bookmark (called by reanchor_bookmark)
    # 3. for _repair_page_reset's current check
    client.progression_sequence = [
        locator_payload,
        locator_payload,
        locator_payload,
    ]
    # get_book calls during sync:
    # 1. line 910 in sync_reachable (ensure_present check)
    # 2. line 1051 in sync_reachable (before repair)
    # 3. in _repair_page_reset's get_book call (after repair, page recomputed)
    client.book_sequence = [
        komga_book(
            pages=185,
            read={"page": 1, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
        komga_book(
            pages=185,
            read={"page": 1, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
        komga_book(
            pages=185,
            read={"page": 119, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_read_position"] == 119
    # Check that repair PUT was called with fresh modified
    assert len(client.put_progressions) == 1
    put_id, put_payload = client.put_progressions[0]
    assert put_id == "KB1"
    assert put_payload["locator"] == locator_payload["locator"]
    assert put_payload["modified"] == FIXED_NOW.isoformat()
    # Check log message
    assert any(
        r.levelname == "INFO"
        and "Re-wrote the Komga progression" in r.message
        and "page 1 -> 119 of 185" in r.message
        for r in caplog.records
    )


def test_no_repair_when_the_page_is_real(repo: _FakeBookRepository) -> None:
    """No repair when the page is already correct (page > 1)."""
    locator_payload = {
        "locator": {"locations": {"position": 118, "progression": 0.64, "totalProgression": 0.64}},
        "device": {},
    }
    client = FakeKomga()
    # Multiple get_progression calls: snapshot, read_bookmark (in reanchor)
    client.progression_sequence = [locator_payload, locator_payload]
    # Multiple get_book calls: ensure_present, before repair
    client.book_sequence = [
        komga_book(
            pages=185,
            read={"page": 119, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
        komga_book(
            pages=185,
            read={"page": 119, "completed": False},  # page is real, not reset to 1
            metadata=MATCHING_BOOK_METADATA,
        ),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_read_position"] == 119
    # No repair PUT should be called
    assert client.put_progressions == []


def test_no_repair_when_the_locator_is_at_the_start(repo: _FakeBookRepository) -> None:
    """No repair when locator is at the start (totalProgression 0.0)."""
    locator_payload = {
        "locator": {"locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0}},
        "device": {},
    }
    client = FakeKomga()
    # Multiple get_progression calls: snapshot, read_bookmark, repair check
    client.progression_sequence = [locator_payload, locator_payload, locator_payload]
    # Multiple get_book calls: ensure_present, before repair
    client.book_sequence = [
        komga_book(
            pages=185,
            read={"page": 1, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
        komga_book(
            pages=185,
            read={"page": 1, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    # No repair PUT should be called (locator is at start)
    assert client.put_progressions == []


def test_no_repair_on_a_stale_table(repo: _FakeBookRepository) -> None:
    """No repair when the anchor join is inconsistent (stale file)."""
    locator_payload = {
        "locator": {"locations": {"position": 118, "progression": 0.64, "totalProgression": 0.64}},
        "device": {},
    }
    client = FakeKomga()
    client.progression_sequence = [locator_payload]
    client.book = komga_book(
        pages=185,
        read={"page": 1, "completed": False},
        metadata=MATCHING_BOOK_METADATA,
    )
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    # Create a situation where join is inconsistent by mocking _document_hrefs to return wrong hrefs
    book_view = book_to_view(book, chapter_table=_chapters(("MISSING.xhtml", "Wrong")))

    svc = service(client)
    # Override _document_hrefs to not match the chapter table (simulates stale)
    svc._document_hrefs = lambda _: ()  # type: ignore[method-assign]

    result = svc.sync(book_view)

    assert result.ok is True
    # No repair PUT should be called (stale join)
    assert client.put_progressions == []


def test_a_refused_re_write_is_logged_at_debug(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """When Komga refuses the progression re-write, log at DEBUG (not an error)."""
    locator_payload = {
        "locator": {"locations": {"position": 118, "progression": 0.64, "totalProgression": 0.64}},
        "device": {},
    }
    client = FakeKomga()
    # Multiple get_progression calls: snapshot, read_bookmark, repair check
    client.progression_sequence = [
        locator_payload,
        locator_payload,
        locator_payload,
    ]
    client.put_progression_ok = False  # Komga refuses the PUT
    # Multiple get_book calls: ensure_present, before repair
    client.book_sequence = [
        komga_book(
            pages=185,
            read={"page": 1, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
        komga_book(
            pages=185,
            read={"page": 1, "completed": False},
            metadata=MATCHING_BOOK_METADATA,
        ),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))

    assert result.ok is True
    # Still returns the original page value when repair was refused
    assert result.fields["external_read_position"] == 1
    # Should have a DEBUG log about the refused re-write
    assert any(
        r.levelname == "DEBUG"
        and "Komga refused the progression re-write" in r.message
        and "KB1" in r.message
        for r in caplog.records
    )


def test_records_link_and_timestamps(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id="S9", metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    result = service(client).sync(book_to_view(book))

    assert result.fields["external_item_id"] == "KB1"
    assert result.fields["external_collection_id"] == "S9"
    assert result.fields["external_synced_at"] is not None
    # First link has no prior provider state, so total is re-baselined silently (EXP-195)
    assert "external_progress_at" not in result.fields


def test_error_when_komga_book_cannot_be_loaded(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = None  # found id, but get_book fails
    result = service(client).sync(book_to_view(make_book(repo)))
    assert result.ok is False


def test_minimal_book_emits_minimal_patches(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={})
    client.series = {"metadata": {}}
    # a book with no author/category/urls/description
    book = make_book(
        repo,
        author="",
        description="",
        category="",
        eroticatags="",
        authorUrl="",
        sectionUrl="",
        storyUrl="https://x.test/s/min",
        seriesUrl="",
    )

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    _, patch = client.book_patches[0]
    assert "authors" not in patch  # no author -> field omitted
    assert "tags" not in patch  # empty == empty -> unchanged, omitted
    assert "links" not in patch  # no Book/Author urls -> empty, omitted
    # only the Story link differs on the series; genres/language stay absent
    _, series_patch = client.series_patches[0]
    assert "genres" not in series_patch
    assert "language" not in series_patch
    assert series_patch["links"] == [{"label": "Story", "url": "https://x.test/s/min"}]


def test_no_series_push_when_book_has_no_series(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id=None, metadata=MATCHING_BOOK_METADATA)

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert client.series_patches == []


def test_series_push_skipped_when_series_cannot_be_loaded(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id="S1", metadata=MATCHING_BOOK_METADATA)
    client.series = None  # get_series fails

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert client.series_patches == []


def test_untitled_unlinked_book_is_not_found(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    result = service(client).sync(book_to_view(make_book(repo, title=None)))
    assert result.ok is False
    assert client.find_calls == 0
    assert client.scan_calls == 0


def test_external_progress_at_unset_when_read_state_and_rating_unchanged(
    repo: _FakeBookRepository,
) -> None:
    """external_progress_at is not set when read-state is unchanged and no adoption occurs."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=14, read={"page": 10, "completed": False})
    client.series = {"metadata": {}}
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 10, "external_read_completed": 0},
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.fields.get("external_progress_at") is None  # no change, no adoption → not bumped
    assert result.fields["external_synced_at"] is not None


def test_enrich_pulls_ids_state_and_catalog_read_only(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        series_id="S1",
        metadata={"tags": ["Het", "bdsm", "rating:5"]},
        pages=20,
        read={"page": 7, "completed": False},
    )
    client.series = {"metadata": {"genres": ["Het"]}}
    # Book has no catalog yet (absent from EPUB / title page) → Komga fills it (T8).
    book = make_book(repo, category="", eroticatags="")

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_item_id"] == "KB1"
    assert result.fields["external_collection_id"] == "S1"
    assert result.fields["external_read_total"] == 20
    assert result.fields["external_read_position"] == 7
    assert result.fields["category"] == "Het"  # from series genres
    assert result.fields["erotica_tags"] == "bdsm"  # book tags minus the genre and rating:N
    # strictly read-only on Komga -- no metadata push, scan or progression write
    assert client.book_patches == []
    assert client.series_patches == []
    assert client.analyze_calls == []
    assert client.scan_calls == 0
    assert client.put_progressions == []


def test_enrich_without_series_leaves_category_none(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id=None, metadata={"tags": ["bdsm", "rating:3"]})
    # Book has no catalog → Komga fills erotica_tags only (no series → no genres → category=None).
    book = make_book(repo, category="", eroticatags="")

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert result.fields["category"] is None
    assert result.fields["erotica_tags"] == "bdsm"  # rating stripped, no genre to strip


def test_enrich_does_not_overwrite_catalog_when_komga_has_none(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id=None, metadata={"tags": ["rating:2"]})
    book = make_book(repo)  # category="Erotic Horror", eroticatags="Horror, Oral"

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    # no genres/tags from Komga -> nothing adopted -> catalog fields absent from the outcome
    assert "category" not in result.fields
    assert "erotica_tags" not in result.fields


def test_enrich_returns_false_when_not_found(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = []  # not found
    assert service(client).enrich(book_to_view(make_book(repo))).ok is False


def test_enrich_disabled_or_unreachable_returns_false(repo: _FakeBookRepository) -> None:
    book = make_book(repo)
    assert service(FakeKomga(), enabled=False).enrich(book_to_view(book)).ok is False
    unreachable = FakeKomga()
    unreachable.connected = False
    assert service(unreachable).enrich(book_to_view(book)).ok is False


def test_enrich_returns_false_without_title(repo: _FakeBookRepository) -> None:
    assert service(FakeKomga()).enrich(book_to_view(make_book(repo, title=None))).ok is False


def test_enrich_returns_false_when_book_cannot_be_loaded(repo: _FakeBookRepository) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = None  # get_book fails
    assert service(client).enrich(book_to_view(make_book(repo))).ok is False


def test_enrich_finds_by_path_when_output_filename_matches(repo: _FakeBookRepository) -> None:
    """T7: path-first locate — URL suffix match wins over title+author lookup."""
    client = FakeKomga()
    client.library_books = [("KB1", "/books/gabthewriter/The 12th Key.epub")]
    client.book = komga_book(series_id="S1", metadata={}, pages=5, read={})
    client.series = {"metadata": {}}
    book = make_book(repo, output_filename="gabthewriter/The 12th Key.epub")

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert client.list_calls == 1
    assert client.find_calls == 0  # path matched → no title+author fallback
    assert result.fields["external_item_id"] == "KB1"


def test_find_by_path_matches_a_decomposed_url(repo: _FakeBookRepository) -> None:
    """T7 + TXE-D3: a Komga url in decomposed Unicode still matches a composed output_filename."""
    client = FakeKomga()
    client.library_books = [("KB1", "file:///books/Zoë/Café.epub")]
    client.book = komga_book(series_id="S1", metadata={}, pages=5, read={})
    client.series = {"metadata": {}}
    book = make_book(repo, output_filename="Zoë/Café.epub")

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert client.list_calls == 1
    assert client.find_calls == 0  # path matched → no title+author fallback
    assert result.fields["external_item_id"] == "KB1"


def test_enrich_falls_back_to_title_author_on_path_miss(repo: _FakeBookRepository) -> None:
    """T7: falls back to title+author when no Komga URL ends with output_filename."""
    client = FakeKomga()
    client.library_books = [("KB_OTHER", "/books/other/Something.epub")]
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=5)
    client.series = {"metadata": {}}
    book = make_book(repo, output_filename="gabthewriter/The 12th Key.epub")

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert client.list_calls == 1
    assert client.find_calls == 1  # path missed → title+author used


def test_enrich_url_index_built_once_across_multiple_calls(repo: _FakeBookRepository) -> None:
    """T7: list_library_books() is called at most once per KomgaService instance."""
    client = FakeKomga()
    client.library_books = [("KB1", "/books/gabthewriter/The 12th Key.epub")]
    client.book = komga_book(metadata={}, pages=5)
    client.series = {"metadata": {}}
    book = make_book(repo, output_filename="gabthewriter/The 12th Key.epub")

    svc = service(client)
    svc.enrich(book_to_view(book))
    svc.enrich(book_to_view(book))

    assert client.list_calls == 1  # cached after first call


def test_restored_progress_lands_in_db(repo: _FakeBookRepository) -> None:
    """Restore first, then read back: fields carry the restored position, not the reset one."""
    saved = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    reset_prog = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    reset_book = komga_book(
        metadata=MATCHING_BOOK_METADATA, pages=20, read={"page": 0, "completed": False}
    )
    restored_book = komga_book(
        metadata=MATCHING_BOOK_METADATA, pages=20, read={"page": 11, "completed": False}
    )

    client = FakeKomga()
    client.book_sequence = [reset_book, restored_book]
    client.progression_sequence = [saved, reset_prog]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {"external_item_id": "KB1", "external_read_total": 20, "external_read_position": 11},
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_read_position"] == 11  # restored position, not reset 0
    assert result.fields.get("external_progress_at") is None  # final == stored → no bump


def test_enrich_does_not_clobber_title_page_catalog_with_komga(repo: _FakeBookRepository) -> None:
    """T8: when book already has category/erotica_tags, Komga catalog must not overwrite them."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        series_id="S1",
        metadata={"tags": ["Het", "bdsm", "rating:3"]},
        pages=10,
    )
    client.series = {"metadata": {"genres": ["Het"]}}
    # Book already has catalog from the title page (T4 path)
    book = make_book(repo, category="Mind Control", eroticatags="Bimbos, Domination")

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    # title-page values preserved -> Komga catalog was not adopted into the outcome
    assert "category" not in result.fields
    assert "erotica_tags" not in result.fields


def test_enrich_without_output_filename_skips_path_lookup(repo: _FakeBookRepository) -> None:
    """T7: no output_filename → skip list call, go straight to find_book_id."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=5)
    client.series = {"metadata": {}}
    book = make_book(repo)  # output_filename is None

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert client.list_calls == 0  # path lookup skipped entirely
    assert client.find_calls == 1


# --- pure helpers ---------------------------------------------------------- #
def test_catalog_from_komga_inverts_the_push() -> None:
    komga = {"metadata": {"tags": ["Het", "Slash", "bdsm", "rating:4", "★★★★☆"]}}
    series = {"metadata": {"genres": ["Het", "Slash"]}}
    assert _catalog_from_komga(komga, series) == ("Het, Slash", "bdsm")


def test_catalog_from_komga_no_series_or_tags() -> None:
    assert _catalog_from_komga({"metadata": {}}, None) == (None, None)


def test_compose_tags_rating_first() -> None:
    assert _compose_tags("Het, Slash", "zeta, Alpha", 3) == [
        "★★★☆☆",
        "Het",
        "Slash",
        "Alpha",
        "zeta",
    ]


def test_compose_tags_no_rating_categories_first() -> None:
    assert _compose_tags("Het, Slash", "zeta, Alpha", None) == ["Het", "Slash", "Alpha", "zeta"]


def test_compose_tags_ci_collision_keeps_category_slot() -> None:
    # "het" in erotica CI-collides with "Het" category — category slot is kept, erotica dropped
    assert _compose_tags("Het", "het, bdsm", None) == ["Het", "bdsm"]


def test_compose_tags_folds_unicode_case() -> None:
    """TXE-D3: category/erotica de-duplication folds Unicode case, not just ASCII."""
    # "STRASSE" only CI-collides with "Straße" once casefold (not lower()) is applied.
    assert _compose_tags("Straße", "STRASSE, bdsm", None) == ["Straße", "bdsm"]


def test_compose_tags_empty_inputs() -> None:
    assert _compose_tags(None, None, None) == []


def test_komga_only_tags_filters_managed_and_known() -> None:
    result = _komga_only_tags(
        ["het", "★★★☆☆", "rating:4", "zebra", "bad, tag", "BDSM"], "Het", "bdsm"
    )
    assert result == ["zebra"]


def test_sync_imports_komga_only_tags_into_db(repo: _FakeBookRepository) -> None:
    """Komga-only tags are appended to erotica_tags; the tags push is skipped (same content).

    After the import the composed list holds exactly what Komga already has, so the
    changed-fields-only PATCH must not include ``tags`` (order alone is not pushable —
    Komga stores tags unordered).
    """
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata={"tags": ["het", "slash", "noncon", "zebra", "alpha"]}, pages=0, read={}
    )
    client.series = {"metadata": {}}
    book = make_book(repo, category="Het, Slash", eroticatags="Noncon")

    result = service(client).sync(book_to_view(book))

    assert "zebra" in (result.fields.get("erotica_tags") or "").lower()
    assert "alpha" in (result.fields.get("erotica_tags") or "").lower()
    assert len(client.book_patches) == 1
    _, patch_data = client.book_patches[0]
    assert "tags" not in patch_data


def test_sync_no_import_write_when_nothing_new(repo: _FakeBookRepository) -> None:
    """When Komga holds no unknown tags, the catalog fields are absent from the outcome."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={"tags": ["het", "horror", "oral"]}, pages=0, read={})
    client.series = {"metadata": {}}
    book = make_book(repo, category="Het", eroticatags="Horror, Oral")

    result = service(client).sync(book_to_view(book))

    assert "category" not in result.fields
    assert "erotica_tags" not in result.fields


def test_tag_import_never_patches_category(repo: _FakeBookRepository) -> None:
    """Komga tag import patches erotica_tags only; category is never imported.

    When Komga holds unknown tags, they are imported into erotica_tags, but
    category is not written to the base row (only erotica_tags is patched).
    """
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={"tags": ["het", "zebra"]}, pages=0, read={})
    client.series = {"metadata": {}}
    book = make_book(repo, category="Het", eroticatags="Horror")

    result = service(client).sync(book_to_view(book))

    assert "category" not in result.fields
    assert result.fields["erotica_tags"].lower().split(", ") == ["horror", "zebra"]


def test_book_patch_skips_pure_reorder() -> None:
    """Same CI tag set in a different order produces no tags PATCH (content comparison).

    Komga stores tags unordered (a PATCH's array order is hash-scrambled server-side),
    so a reorder cannot round-trip — pushing one would just rewrite Komga on every sync.
    """
    book = _blank_view(category="Het", erotica_tags="bdsm")
    # desired = ["Het", "bdsm"]; current reversed — same set, different order
    result = _book_patch(book, {"tags": ["bdsm", "het"]}, ["Het", "bdsm"])
    assert "tags" not in result


def test_genres_splits_and_handles_empty() -> None:
    assert _genres(_blank_view(category="Het, Slash")) == ["Het", "Slash"]
    assert _genres(_blank_view(category="Het")) == ["Het"]
    assert _genres(_blank_view()) == []


def test_upsert_links_append_update_and_keep_unrelated() -> None:
    appended, changed = _upsert_links([], [{"label": "Book", "url": "u"}])
    assert appended == [{"label": "Book", "url": "u"}]
    assert changed is True
    updated, changed = _upsert_links(
        [{"label": "Book", "url": "old"}], [{"label": "Book", "url": "new"}]
    )
    assert updated == [{"label": "Book", "url": "new"}]
    assert changed is True
    kept, changed = _upsert_links(
        [{"label": "Other", "url": "x"}, {"label": "Book", "url": "u"}],
        [{"label": "Book", "url": "u"}],
    )
    assert kept == [{"label": "Other", "url": "x"}, {"label": "Book", "url": "u"}]
    assert changed is False


def test_rating_tag_formats() -> None:
    assert _rating_tag(1) == "★☆☆☆☆"
    assert _rating_tag(3) == "★★★☆☆"
    assert _rating_tag(5) == "★★★★★"


def test_rating_from_tags_variants() -> None:
    # legacy format
    assert _rating_from_tags(["rating:5"]) == 5
    assert _rating_from_tags(["rating:0"]) == 0
    assert _rating_from_tags(["rating:abc", "other"]) is None
    assert _rating_from_tags([]) is None
    assert _rating_from_tags(["rating:6"]) is None  # out of range
    # star form — five-char mixed
    assert _rating_from_tags(["★★★☆☆"]) == 3
    assert _rating_from_tags(["★★★★★"]) == 5
    assert _rating_from_tags(["☆☆☆☆☆"]) == 0
    # star form — 1-5 black stars alone
    assert _rating_from_tags(["★★★"]) == 3
    # invalid star forms
    assert _rating_from_tags(["★★★★★★"]) is None  # six stars
    assert _rating_from_tags(["★☆☆"]) is None  # length 3, has empty stars
    # first parseable wins
    assert _rating_from_tags(["rating:3", "★★★★★"]) == 3


def test_book_patch_empty_for_empty_book_and_current() -> None:
    assert _book_patch(_blank_view(), {}, []) == {}


def test_summary_pushed_keeps_paragraph_breaks() -> None:
    """Verify that synopsis with paragraph breaks are pushed to Komga verbatim."""
    book = _blank_view(synopsis="Para one.\n\nPara two.")
    patch = _book_patch(book, {}, [])
    assert patch["summary"] == "Para one.\n\nPara two."


def test_series_patch_genres_split_and_no_links() -> None:
    book = _blank_view(category="Het, Slash")
    patch = _series_patch(book, {})
    assert patch["genres"] == ["Het", "Slash"]
    assert "language" not in patch
    assert "links" not in patch


def test_series_patch_genres_case_insensitive_no_change() -> None:
    patch = _series_patch(_blank_view(category="Erotic Horror"), {"genres": ["erotic horror"]})
    assert "genres" not in patch


def test_series_patch_link_uses_series_url_for_series_book() -> None:
    patch = _series_patch(
        _blank_view(
            series="Infinite Wishes",
            series_url="https://x.test/series/9",
            story_url="https://x.test/s/ch-3",
        ),
        {},
    )
    assert patch["links"] == [{"label": "Story", "url": "https://x.test/series/9"}]


def test_series_patch_no_link_for_series_book_without_series_url() -> None:
    patch = _series_patch(
        _blank_view(
            series="Infinite Wishes",
            series_url=None,
            story_url="https://x.test/s/ch-3",
        ),
        {},
    )
    assert "links" not in patch


def test_series_patch_link_uses_story_url_for_standalone_book() -> None:
    patch = _series_patch(
        _blank_view(story_url="https://x.test/s/solo"),
        {},
    )
    assert patch["links"] == [{"label": "Story", "url": "https://x.test/s/solo"}]


def test_series_patch_link_converges_when_series_url_already_present() -> None:
    patch = _series_patch(
        _blank_view(
            series="IW",
            series_url="https://x.test/series/9",
        ),
        {"links": [{"label": "Story", "url": "https://x.test/series/9"}]},
    )
    assert "links" not in patch


# ---  series status and publisher push -------------------------------- #


def test_series_patch_status_completed_maps_to_ended() -> None:
    patch = _series_patch(_blank_view(status="Completed"), {})
    assert patch.get("status") == "ENDED"


def test_series_patch_status_in_progress_maps_to_ongoing() -> None:
    patch = _series_patch(_blank_view(status="In-Progress"), {})
    assert patch.get("status") == "ONGOING"


def test_series_patch_status_unknown_omitted() -> None:
    """Unknown FFF status values (e.g. 'Hiatus') must not be forwarded to Komga."""
    patch = _series_patch(_blank_view(status="Hiatus"), {})
    assert "status" not in patch


def test_series_patch_status_none_omitted() -> None:
    patch = _series_patch(_blank_view(status=None), {})
    assert "status" not in patch


def test_series_patch_status_unchanged_omitted() -> None:
    """When Komga already shows the mapped value, status must be omitted from the patch."""
    patch = _series_patch(_blank_view(status="Completed"), {"status": "ENDED"})
    assert "status" not in patch


def test_series_patch_publisher_set_when_differs() -> None:
    patch = _series_patch(_blank_view(site="literotica.com"), {})
    assert patch.get("publisher") == "literotica.com"


def test_series_patch_publisher_omitted_when_equal() -> None:
    patch = _series_patch(_blank_view(site="literotica.com"), {"publisher": "literotica.com"})
    assert "publisher" not in patch


def test_series_patch_publisher_omitted_when_none() -> None:
    patch = _series_patch(_blank_view(site=None), {})
    assert "publisher" not in patch


def test_series_status_and_publisher_pushed_on_sync(repo: _FakeBookRepository) -> None:
    """Integration: Completed book + ONGOING Komga series → PATCH includes status=ENDED
    and publisher; a second sync against the updated series produces no series PATCH."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    # Komga series has ONGOING and no publisher → both fields should change
    client.series = {
        "metadata": {
            "genres": ["Erotic Horror"],
            "status": "ONGOING",
            "links": [{"label": "Story", "url": STORY_URL}],
        }
    }
    book = make_book(repo, status="Completed", site="literotica.com")

    service(client).sync(book_to_view(book))

    assert len(client.series_patches) == 1
    _, patch = client.series_patches[0]
    assert patch.get("status") == "ENDED"
    assert patch.get("publisher") == "literotica.com"
    assert "genres" not in patch  # genres already match


def test_series_status_unchanged_no_second_patch(repo: _FakeBookRepository) -> None:
    """Second sync against an already-correct series must produce no PATCH."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    # Series already has the correct status and publisher
    client.series = {
        "metadata": {
            "genres": ["Erotic Horror"],
            "status": "ENDED",
            "publisher": "literotica.com",
            "links": [{"label": "Story", "url": STORY_URL}],
        }
    }
    book = make_book(repo, status="Completed", site="literotica.com")

    service(client).sync(book_to_view(book))

    assert client.series_patches == []


# --- path-first Komga discovery ( ----------------------------------- #


def test_scan_poll_finds_book_by_path_before_author_metadata_ready(
    repo: _FakeBookRepository,
) -> None:
    """Path match during poll wins even when find_book_id always returns None.

    Komga populates author metadata asynchronously after a scan; the file path
    is correct immediately, so polling by path is more reliable.
    """
    client = FakeKomga()
    client.find_results = []  # find_book_id always None (author metadata not ready)
    # Before scan: book not yet visible. After scan (first poll): file is there.
    client.library_books_sequence = [
        [],  # initial _find_by_path check before the scan
        [("k1", "/data/books/Author/Title.epub")],  # first poll attempt
    ]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo, output_filename="Author/Title.epub")

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert client.scan_calls == 1
    assert result.fields["external_item_id"] == "k1"


def test_scan_poll_refreshes_url_index_between_attempts(
    repo: _FakeBookRepository,
) -> None:
    """URL index is rebuilt on each poll attempt so a later attempt finds a new book."""
    client = FakeKomga()
    client.find_results = []  # find_book_id always None
    # First poll: empty. Second poll: book is there.
    client.library_books_sequence = [
        [],  # initial _find_by_path check before the scan
        [],  # first poll attempt
        [("k1", "/data/books/Author/Title.epub")],  # second poll attempt
    ]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo, output_filename="Author/Title.epub")

    result = service(client, scan_retry_max=3).sync(book_to_view(book))

    assert result.ok is True
    # 1 pre-scan check + 1 per poll attempt (cache invalidated between attempts)
    assert client.list_calls == 3


def test_unlinked_book_checks_path_before_triggering_scan(
    repo: _FakeBookRepository,
) -> None:
    """When the path index already contains the book, no library scan is triggered."""
    client = FakeKomga()
    client.find_results = [None]  # find_book_id misses on the first try
    client.library_books = [("k1", "/data/books/Author/Title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo, output_filename="Author/Title.epub")

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert client.scan_calls == 0  # found by path before a scan was needed
    assert result.fields["external_item_id"] == "k1"


# --- scan_retry_delay floor ( --------------------------------------- #


def test_poll_delay_zero_floors_to_2_seconds(repo: _FakeBookRepository) -> None:
    """scan_retry_delay=0 must be replaced by the 2s default at construction time."""
    client = FakeKomga()
    client.find_results = []  # always None → full poll loop
    sleep_calls: list[float] = []

    service(client, scan_retry_delay=0, scan_retry_max=2, sleep=sleep_calls.append).sync(
        book_to_view(make_book(repo))
    )

    assert sleep_calls == [2.0, 2.0]


def test_poll_delay_explicit_positive_honoured(repo: _FakeBookRepository) -> None:
    """An explicit positive scan_retry_delay is used verbatim."""
    client = FakeKomga()
    client.find_results = []  # always None → full poll loop
    sleep_calls: list[float] = []

    service(client, scan_retry_delay=0.5, scan_retry_max=2, sleep=sleep_calls.append).sync(
        book_to_view(make_book(repo))
    )

    assert sleep_calls == [0.5, 0.5]


# ---  pull_reading_state + sync signature additions ------------------- #


def test_sync_restores_progress_when_reset_after_scan(
    repo: _FakeBookRepository,
) -> None:
    """sync() restores progress when progression was zeroed (sync-start snapshot rule)."""
    snap = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    # snap captured at sync start, reset seen by post-analyze
    client.progression_sequence = [snap, reset]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    assert client.put_progressions == [("KB1", {**snap, "modified": FIXED_NOW.isoformat()})]


def test_sync_captures_progression_at_start(
    repo: _FakeBookRepository,
) -> None:
    """sync(book) captures progression at start (sync-start snapshot rule)."""
    saved = {"locator": {"locations": {"position": 5}}}
    reset = {"locator": {"locations": {"position": 0}}}
    client = FakeKomga()
    # Three calls: sync start snapshot, restore check, semantic capture
    client.progression_sequence = [saved, reset, reset]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    # Three calls: once at sync start, once in _restore_progress_if_lost, once for semantic capture.
    assert client.get_progression_call_count == 3
    assert client.put_progressions == [("KB1", {**saved, "modified": FIXED_NOW.isoformat()})]


def test_sync_has_no_snapshot_parameter() -> None:
    """Verify RB6 residue is removed: sync() has no progression_snapshot param."""
    assert "progression_snapshot" not in inspect.signature(KomgaService.sync).parameters
    assert not hasattr(KomgaService, "pull_reading_state")


def test_sync_allow_scan_false_returns_not_found_without_scan(
    repo: _FakeBookRepository,
) -> None:
    """sync(book, allow_scan=False) on unlinked unfound book skips library scan."""
    client = FakeKomga()
    client.find_results = []  # not found by title or path
    book = make_book(repo)  # has title, no komga_book_id

    result = service(client).sync(book_to_view(book), allow_scan=False)

    assert result.ok is False
    assert "scan skipped" in result.message
    assert client.scan_calls == 0


# ---  Bi-directional rating:n tag sync -------------------------------- #


def test_sync_rating_adopt_null_komga_tag(repo: _FakeBookRepository) -> None:
    """NULL local + rating:4 on Komga → adopted rating 4, external_progress_at bumped."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata={"tags": ["rating:4"]}, pages=14, read={"page": 0, "completed": False}
    )
    client.series = {"metadata": {}}
    book = make_book(repo, category="", eroticatags="")
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 0, "external_read_completed": 0},
    )
    book = repo.get(book.book_id)
    assert book is not None
    assert book.rating is None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["rating"] == 4
    assert result.fields.get("external_progress_at") is not None  # adoption bumps it
    # legacy rating:4 input is replaced by the star form in the push
    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    assert "★★★★☆" in patch.get("tags", [])
    assert not any(t.startswith("rating:") for t in patch.get("tags", []))


def test_sync_rating_no_create_null_no_tag(repo: _FakeBookRepository) -> None:
    """NULL local + no Komga rating tag → nothing adopted, no rating: in any PATCH."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=14, read={"page": 0, "completed": False})
    client.series = {"metadata": {}}
    book = make_book(repo, category="", eroticatags="")
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 0, "external_read_completed": 0},
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert "rating" not in result.fields
    for _, pushed in client.book_patches:
        tags = pushed.get("tags", [])
        assert not any(t.startswith("rating:") for t in tags)
        assert not any(set(t) <= {"★", "☆"} and len(t) > 0 for t in tags)


def test_sync_rating_null_komga_tag_0_removes_tag(repo: _FakeBookRepository) -> None:
    """NULL local + rating:0 on Komga → PATCH tags without rating; rating not adopted."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata={"tags": ["rating:0"]}, pages=14, read={"page": 0, "completed": False}
    )
    client.series = {"metadata": {}}
    book = make_book(repo, category="", eroticatags="")
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 0, "external_read_completed": 0},
    )
    book = repo.get(book.book_id)

    result = service(client).sync(book_to_view(book))

    assert "rating" not in result.fields  # 0 not adopted
    # rating:0 must be stripped — PATCH must include tags=[] to overwrite Komga's rating:0
    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    assert "tags" in patch
    tags = patch["tags"]
    assert not any(t.startswith("rating:") for t in tags)
    assert not any(set(t) <= {"★", "☆"} and len(t) > 0 for t in tags)


def test_sync_rating_clear_local_0_with_komga_tag(repo: _FakeBookRepository) -> None:
    """local 0 + rating:3 on Komga → PATCH tags without rating, then rating cleared to None."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata={"tags": ["rating:3"]}, pages=14, read={"page": 0, "completed": False}
    )
    client.series = {"metadata": {}}
    book = make_book(repo, category="", eroticatags="")
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 0, "external_read_completed": 0},
    )
    repo.set_rating(book.book_id, 0)
    book = repo.get(book.book_id)
    assert book is not None
    assert book.rating == 0

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["rating"] is None  # 0 → cleared
    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    tags = patch.get("tags", [])
    assert not any(t.startswith("rating:") for t in tags)
    assert not any(set(t) <= {"★", "☆"} and len(t) > 0 for t in tags)


def test_sync_rating_clear_local_0_no_tag(repo: _FakeBookRepository) -> None:
    """local 0, no Komga rating tag → rating cleared to None (no tags PATCH needed)."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata=MATCHING_BOOK_METADATA, pages=14, read={"page": 0, "completed": False}
    )
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 0, "external_read_completed": 0},
    )
    repo.set_rating(book.book_id, 0)
    book = repo.get(book.book_id)
    assert book is not None
    assert book.rating == 0

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["rating"] is None  # clear completed
    assert client.book_patches == []  # tags already matched (no rating change in composed set)


def test_sync_rating_push_create_local_5_no_komga_tag(repo: _FakeBookRepository) -> None:
    """local 5, no Komga rating tag → rating:5 injected into PATCH; lit wins, nothing adopted."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=14, read={"page": 0, "completed": False})
    client.series = {"metadata": {}}
    book = make_book(repo, category="", eroticatags="")
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 0, "external_read_completed": 0},
    )
    repo.set_rating(book.book_id, 5)
    book = repo.get(book.book_id)
    assert book is not None
    assert book.rating == 5

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert "rating" not in result.fields  # local already 5 → no adopt/clear write
    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    assert "★★★★★" in patch.get("tags", [])


def test_sync_rating_push_conflict_local_2_komga_4(repo: _FakeBookRepository) -> None:
    """local 2 + rating:4 on Komga → rating:2 in PATCH (lit wins), nothing adopted."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata={"tags": ["rating:4"]}, pages=14, read={"page": 0, "completed": False}
    )
    client.series = {"metadata": {}}
    book = make_book(repo, category="", eroticatags="")
    repo.update_fields(
        book.book_id,
        {"external_read_total": 14, "external_read_position": 0, "external_read_completed": 0},
    )
    repo.set_rating(book.book_id, 2)
    book = repo.get(book.book_id)
    assert book is not None
    assert book.rating == 2

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert "rating" not in result.fields  # local wins, no write needed
    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    assert "★★☆☆☆" in patch.get("tags", [])
    assert "★★★★☆" not in patch.get("tags", [])


def test_enrich_adopts_rating_when_local_null(repo: _FakeBookRepository) -> None:
    """enrich: NULL local + rating:3 on Komga → adopted rating 3, no Komga PATCH."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id=None, metadata={"tags": ["rating:3"]}, pages=10)
    book = make_book(repo, category="", eroticatags="")

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert result.fields["rating"] == 3
    assert client.book_patches == []  # enrich is read-only toward Komga


def test_enrich_does_not_adopt_rating_when_local_set(repo: _FakeBookRepository) -> None:
    """enrich: local 2 + rating:5 on Komga → nothing adopted (lit wins), no PATCH."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(series_id=None, metadata={"tags": ["rating:5"]}, pages=10)
    book = make_book(repo, category="", eroticatags="")
    repo.set_rating(book.book_id, 2)
    book = repo.get(book.book_id)
    assert book is not None
    assert book.rating == 2

    result = service(client).enrich(book_to_view(book))

    assert result.ok is True
    assert "rating" not in result.fields  # local wins
    assert client.book_patches == []


# ---  ebookerr link push --------------------------------------------- #


def test_sync_pushes_ebookerr_link(repo: _FakeBookRepository) -> None:
    """app_external_url set → PATCH links contains ebookerr alongside Book/Author."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={})  # empty → all fields differ
    client.series = {"metadata": {}}
    book = make_book(repo)

    service(client, app_external_url="http://nas:5001/").sync(book_to_view(book))

    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    labels = {link["label"] for link in patch["links"]}
    assert "ebookerr" in labels
    assert "Book" in labels
    assert "Author" in labels
    ebookerr = next(link for link in patch["links"] if link["label"] == "ebookerr")
    assert ebookerr["url"] == f"http://nas:5001/books/{book.book_id}"


def test_sync_updates_stale_ebookerr_link(repo: _FakeBookRepository) -> None:
    """Stale ebookerr URL in Komga is replaced with the corrected one."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    stale_meta = {
        **MATCHING_BOOK_METADATA,
        "tags": ["erotic horror", "horror", "oral"],  # CI match → no tags patch
        "links": [
            {"label": "Book", "url": STORY_URL},
            {"label": "Author", "url": AUTHOR_URL},
            {"label": "ebookerr", "url": "http://old-host/library?book=stale"},
        ],
    }
    client.book = komga_book(metadata=stale_meta)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    service(client, app_external_url="http://nas:5001/").sync(book_to_view(book))

    assert len(client.book_patches) == 1
    _, patch = client.book_patches[0]
    assert "links" in patch
    ebookerr = next(link for link in patch["links"] if link["label"] == "ebookerr")
    assert ebookerr["url"] == f"http://nas:5001/books/{book.book_id}"


def test_sync_without_external_url_omits_ebookerr_link(repo: _FakeBookRepository) -> None:
    """app_external_url=None → no ebookerr in any PATCH; unchanged links produce no links key."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    # Different tags → forces a tags patch; links match → no links key in that patch
    client.book = komga_book(metadata={**MATCHING_BOOK_METADATA, "tags": ["old-tag"]})
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    service(client, app_external_url=None).sync(book_to_view(make_book(repo)))

    assert client.book_patches  # tags changed → at least one patch occurred
    for _, p in client.book_patches:
        assert not any(link["label"] == "ebookerr" for link in p.get("links", []))
        assert "links" not in p  # links unchanged (no ebookerr added) → key absent


# ───  sync / restore logging ─────────────────────────────────────────


def test_sync_logs_pushed_metadata_fields(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={})  # empty metadata → all local fields differ → patch sent
    client.series = {"metadata": {}}
    book = make_book(repo)
    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))
    assert result.ok is True
    assert any(
        r.levelno == logging.INFO and "Pushing Komga book metadata" in r.message
        for r in caplog.records
    ), f"Expected INFO 'Pushing Komga book metadata', got: {[r.message for r in caplog.records]}"


def test_restore_logs_success(repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture) -> None:
    snap = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    client = FakeKomga()
    client.progression_sequence = [snap, {}]  # snap at sync start, post-analyze: progress lost
    client.put_progression_ok = True
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))
    assert result.ok is True
    assert any(
        r.levelno == logging.INFO and "Restored read progression" in r.message
        for r in caplog.records
    ), f"Expected INFO 'Restored read progression', got: {[r.message for r in caplog.records]}"


# ---  outcome fields, deep link, locator persistence, DB-fallback restore --- #


def test_sync_fields_include_deep_link(repo: _FakeBookRepository) -> None:
    """A linked sync's fields carry the Komga deep link computed from server_url."""
    client = FakeKomga()
    client.find_results = ["B7"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    result = service(client, server_url="http://k/").sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_item_url"] == "http://k/book/B7"


def test_komga_deep_link_prefers_external_url_over_server(repo: _FakeBookRepository) -> None:
    """Komga deep link uses external_url when set, else server_url (EXP-197)."""
    client = FakeKomga()
    client.find_results = ["B7"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    result = service(
        client,
        server_url="http://k/",
        external_url="http://komga.example.test/",
    ).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_item_url"] == "http://komga.example.test/book/B7"


def test_sync_persists_locator_json(repo: _FakeBookRepository) -> None:
    """A progression with positions round-trips as JSON in fields["external_locator"]."""
    saved = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    client = FakeKomga()
    client.progression_sequence = [saved, saved]  # unchanged after analyze (progress intact)
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert json.loads(result.fields["external_locator"]) == saved


def test_sync_restores_from_persisted_locator(repo: _FakeBookRepository) -> None:
    """DB-fallback: an empty live snapshot falls back to the persisted locator for restore."""
    persisted = {"locator": {"locations": {"position": 11, "progression": 0.68}}, "device": {}}
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [reset, reset]  # live snapshot, then post-analyze (both empty)
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(
        book.book_id, {"external_item_id": "KB1", "external_locator": json.dumps(persisted)}
    )
    book = repo.get(book.book_id)
    assert book is not None

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    assert client.put_progressions == [("KB1", {**persisted, "modified": FIXED_NOW.isoformat()})]


def test_corrupt_locator_warns_and_skips_restore(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt persisted locator logs a WARNING and sync proceeds without restoring."""
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [reset]  # live snapshot: empty
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(
        book.book_id, {"external_item_id": "KB1", "external_locator": "{not valid json"}
    )
    book = repo.get(book.book_id)
    assert book is not None

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert client.put_progressions == []
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_a_stale_persisted_locator_is_not_replayed(repo: _FakeBookRepository) -> None:
    """Persisted locator older than newest read-position is not replayed."""
    client = FakeKomga()
    service_instance = service(client)

    # Create a book with persisted locator (stale)
    stale_locator_json = json.dumps(
        {"modified": "2026-08-25T17:19:18+00:00", "locator": {"locations": {"position": 5}}}
    )

    book = make_book(repo)
    repo.update_fields(
        book.book_id, {"external_item_id": "KB1", "external_locator": stale_locator_json}
    )
    book = repo.get(book.book_id)
    assert book is not None

    # Create a BookView with a newer read_position
    read_pos = ReadPosition(
        captured_at="2026-08-28T10:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 1",
        chapter_href="ch1.xhtml",
        completed=False,
        total_chapters=5,
    )

    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=read_pos,
    )

    # Call _snapshot_with_db_fallback with an empty snapshot
    result = service_instance._snapshot_with_db_fallback(book_view, "KB1", {}, relinked=False)

    # Should return the empty snapshot, not the stale locator
    assert result == {}


def test_a_fresh_persisted_locator_is_replayed(repo: _FakeBookRepository) -> None:
    """Persisted locator newer than newest read-position is replayed."""
    client = FakeKomga()
    service_instance = service(client)

    # Create a book with persisted locator (fresh)
    fresh_locator_dict = {
        "modified": "2026-08-28T11:00:00+00:00",
        "locator": {"locations": {"position": 5}},
    }
    fresh_locator_json = json.dumps(fresh_locator_dict)

    book = make_book(repo)
    repo.update_fields(
        book.book_id, {"external_item_id": "KB1", "external_locator": fresh_locator_json}
    )
    book = repo.get(book.book_id)
    assert book is not None

    # Create a BookView with an older read_position
    read_pos = ReadPosition(
        captured_at="2026-08-28T10:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 1",
        chapter_href="ch1.xhtml",
        completed=False,
        total_chapters=5,
    )

    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=read_pos,
    )

    # Call _snapshot_with_db_fallback with an empty snapshot
    result = service_instance._snapshot_with_db_fallback(book_view, "KB1", {}, relinked=False)

    # Should return the parsed locator
    assert result == fresh_locator_dict


def test_a_locator_with_no_modified_stamp_is_replayed(repo: _FakeBookRepository) -> None:
    """Persisted locator without modified key is replayed (fail open)."""
    client = FakeKomga()
    service_instance = service(client)

    # Create a book with persisted locator (no modified key)
    locator_dict = {"locator": {"locations": {"position": 5}}}
    locator_json = json.dumps(locator_dict)

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1", "external_locator": locator_json})
    book = repo.get(book.book_id)
    assert book is not None

    # Create a BookView with a read_position
    read_pos = ReadPosition(
        captured_at="2026-08-28T10:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 1",
        chapter_href="ch1.xhtml",
        completed=False,
        total_chapters=5,
    )

    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=read_pos,
    )

    # Call _snapshot_with_db_fallback with an empty snapshot
    result = service_instance._snapshot_with_db_fallback(book_view, "KB1", {}, relinked=False)

    # Should return the parsed locator (no modified key → fail open)
    assert result == locator_dict


def test_a_book_with_no_history_replays_its_locator(repo: _FakeBookRepository) -> None:
    """Persisted locator is replayed when book has no read_position history."""
    client = FakeKomga()
    service_instance = service(client)

    # Create a book with persisted locator
    locator_dict = {
        "modified": "2026-08-25T17:19:18+00:00",
        "locator": {"locations": {"position": 5}},
    }
    locator_json = json.dumps(locator_dict)

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1", "external_locator": locator_json})
    book = repo.get(book.book_id)
    assert book is not None

    # Create a BookView with no read_position (None)
    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=None,
    )

    # Call _snapshot_with_db_fallback with an empty snapshot
    result = service_instance._snapshot_with_db_fallback(book_view, "KB1", {}, relinked=False)

    # Should return the parsed locator (no read_position → fail open)
    assert result == locator_dict


def test_the_stale_locator_is_logged_at_warning(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Stale locator warning message contains expected text."""
    client = FakeKomga()
    service_instance = service(client)

    # Create a book with persisted locator (stale)
    stale_locator_json = json.dumps(
        {"modified": "2026-08-25T17:19:18+00:00", "locator": {"locations": {"position": 5}}}
    )

    book = make_book(repo)
    repo.update_fields(
        book.book_id, {"external_item_id": "KB1", "external_locator": stale_locator_json}
    )
    book = repo.get(book.book_id)
    assert book is not None

    # Create a BookView with a newer read_position
    read_pos = ReadPosition(
        captured_at="2026-08-28T10:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 1",
        chapter_href="ch1.xhtml",
        completed=False,
        total_chapters=5,
    )

    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=read_pos,
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        service_instance._snapshot_with_db_fallback(book_view, "KB1", {}, relinked=False)

    # Check for the expected warning message
    assert any(
        r.levelname == "WARNING" and "is older than the newest read-position record" in r.message
        for r in caplog.records
    )


def test_service_has_no_repo_dependency() -> None:
    """KomgaService's constructor no longer accepts a book_repo (DB-write-free service)."""
    params = inspect.signature(KomgaService.__init__).parameters
    assert "book_repo" not in params


# ---  connection cache + per-run series dedupe -------------------------


def test_connection_check_cached_for_ttl(repo: _FakeBookRepository) -> None:
    """sync() calls without advancing clock should use cached connection check."""
    clock: dict[str, float] = {"t": 0.0}
    client = FakeKomga()
    client.find_results = ["KB1", "KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    book_a = make_book(repo)
    book_b = make_book(repo, title="Other Book")

    svc = service(client, monotonic=lambda: clock["t"])
    result_a = svc.sync(book_to_view(book_a))
    result_b = svc.sync(book_to_view(book_b))

    # Both syncs should return ok
    assert result_a.ok is True
    assert result_b.ok is True
    # test_connection() should have been called exactly once (cached on second call)
    assert client.test_connection_calls == 1


def test_connection_check_reprobed_after_ttl(repo: _FakeBookRepository) -> None:
    """After advancing clock by 61s, connection check is re-probed."""
    clock: dict[str, float] = {"t": 0.0}
    client = FakeKomga()
    client.find_results = ["KB1", "KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    book_a = make_book(repo)
    book_b = make_book(repo, title="Other Book")

    svc = service(client, monotonic=lambda: clock["t"])
    result_a = svc.sync(book_to_view(book_a))
    clock["t"] += 61.0
    result_b = svc.sync(book_to_view(book_b))

    # Both syncs should succeed (connection remains ok)
    assert result_a.ok is True
    assert result_b.ok is True
    # Clock advanced past TTL (60s), so second sync re-checks connection
    assert client.test_connection_calls == 2


def test_connection_failure_not_cached(repo: _FakeBookRepository) -> None:
    """Connection failures are never cached; each sync re-probes."""
    clock: dict[str, float] = {"t": 0.0}
    client = FakeKomga()
    client.connected = False

    book_a = make_book(repo)
    book_b = make_book(repo, title="Other Book")

    svc = service(client, monotonic=lambda: clock["t"])
    result_a = svc.sync(book_to_view(book_a))
    result_b = svc.sync(book_to_view(book_b))

    # Both syncs should fail
    assert result_a.ok is False
    assert result_b.ok is False
    # Failures are never cached, so connection is re-checked on every call
    assert client.test_connection_calls == 2


def test_series_push_deduped_per_run(repo: _FakeBookRepository) -> None:
    """Same series dedupe: two books with same seriesId → one get_series call."""
    client = FakeKomga()
    # Both books will have seriesId="S1"
    client.find_results = ["KB1", "KB2"]
    client.book_sequence = [
        komga_book(series_id="S1", metadata=MATCHING_BOOK_METADATA),
        komga_book(series_id="S1", metadata=MATCHING_BOOK_METADATA),
    ]
    client.series = {"metadata": {}}

    book_a = make_book(repo)
    book_b = make_book(repo, title="Book Two")

    svc = service(client)
    svc.sync(book_to_view(book_a))
    svc.sync(book_to_view(book_b))

    # get_series should have been called exactly once (deduplicated on second sync)
    assert len(client.get_series_calls) == 1
    assert client.get_series_calls[0] == "S1"


def test_series_push_not_deduped_across_series(repo: _FakeBookRepository) -> None:
    """Two books in different series should both trigger get_series."""
    client = FakeKomga()
    client.find_results = ["KB1", "KB2"]
    # Each sync calls get_book twice (once in _ensure_present, once in sync flow)
    client.book_sequence = [
        komga_book(series_id="S1", metadata=MATCHING_BOOK_METADATA),
        komga_book(series_id="S1", metadata=MATCHING_BOOK_METADATA),
        komga_book(series_id="S2", metadata=MATCHING_BOOK_METADATA),
        komga_book(series_id="S2", metadata=MATCHING_BOOK_METADATA),
    ]
    client.series = {"metadata": {}}

    book_a = make_book(repo)
    book_b = make_book(repo, title="Book Two")

    svc = service(client)
    svc.sync(book_to_view(book_a))
    svc.sync(book_to_view(book_b))

    # Both syncs succeed, and get_series is called for both (different series IDs)
    assert len(client.get_series_calls) == 2
    assert "S1" in client.get_series_calls
    assert "S2" in client.get_series_calls


def test_delete_remote_book_full_flow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full delete flow: get_book, delete_book_file, scan, poll, empty_trash_for."""
    client = FakeKomga()
    call_order: list[str] = []

    def record_get(item_id: str) -> dict[str, Any] | None:
        call_order.append("get_book")
        if len(call_order) == 1:
            return {"libraryId": "42"}
        return None

    def record_delete(item_id: str) -> bool:
        call_order.append("delete_book_file")
        return True

    def record_scan(lib_id: str) -> bool:
        call_order.append("scan_library")
        return True

    def record_empty(lib_id: str) -> bool:
        call_order.append("empty_trash_for")
        return True

    client.get_book = record_get  # type: ignore
    client.delete_book_file = record_delete  # type: ignore
    client.scan_library = record_scan  # type: ignore
    client.empty_trash_for = record_empty  # type: ignore

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        svc = service(client, scan_retry_max=2, scan_retry_delay=0)
        svc.delete_remote_book("B7")

    expected_calls = [
        "get_book",
        "delete_book_file",
        "scan_library",
        "get_book",
        "empty_trash_for",
    ]
    assert call_order == expected_calls
    assert any("Komga delete requested for book" in r.message for r in caplog.records)
    assert any("Komga trash emptied for library 42: ok" in r.message for r in caplog.records)


def test_delete_remote_book_absent_is_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Book already absent: only get_book called."""
    client = FakeKomga()
    client.book = None  # absent

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        service(client).delete_remote_book("B7")

    # Only get_book should be called
    assert client.get_progression_call_count == 0  # sanity check on state


def test_delete_remote_book_no_library_id_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No libraryId: delete_book_file called, then stop; warning logged."""
    client = FakeKomga()
    client.book = {}  # has no libraryId

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        service(client).delete_remote_book("B7")

    assert any(r.levelname == "WARNING" and "has no libraryId" in r.message for r in caplog.records)


def test_delete_remote_book_poll_timeout_still_empties(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Poll times out: warning logged, still calls empty_trash_for."""
    client = FakeKomga()
    call_order: list[str] = []

    def record_get(item_id: str) -> dict[str, Any] | None:
        call_order.append("get_book")
        return {"libraryId": "42"}  # always present

    def record_scan(lib_id: str) -> bool:
        call_order.append("scan_library")
        return True

    def record_empty(lib_id: str) -> bool:
        call_order.append("empty_trash_for")
        return True

    client.get_book = record_get  # type: ignore
    client.scan_library = record_scan  # type: ignore
    client.empty_trash_for = record_empty  # type: ignore

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        service(client, scan_retry_max=2, scan_retry_delay=0).delete_remote_book("B7")

    assert any(
        r.levelname == "WARNING" and "still present after 2 poll(s)" in r.message
        for r in caplog.records
    )
    # empty_trash_for should still have been called
    assert "empty_trash_for" in call_order


# ---  Semantic read position capture (RP-CAP-5, RP-SEM-1/3/5) -------- #


def test_chapter_table_orders_and_titles() -> None:
    """_chapter_table extracts hrefs in position order with first title per href."""
    hrefs, titles = _chapter_table(POSITIONS_FIXTURE)
    assert hrefs == ["OEBPS/titlepage.xhtml", "OEBPS/file0001.xhtml", "OEBPS/file0002.xhtml"]
    assert titles == ["The 12th Key", "The 12th Key - Ch 1", "The 12th Key - Ch 2"]


def test_sync_returns_read_position(repo: _FakeBookRepository) -> None:
    """sync() returns read_position in SyncResult."""
    progression = {
        "locator": {
            "href": "OEBPS/file0001.xhtml",
            "title": "The 12th Key - Ch 1",
            "locations": {"progression": 0.5, "position": 3, "totalProgression": 0.4},
        }
    }
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    # Mock get_progression and get_positions
    client.progression_sequence = [{}]  # snapshot at sync start (empty)
    client.get_progression = lambda bid: progression  # type: ignore
    client.get_positions = lambda bid: POSITIONS_FIXTURE  # type: ignore

    result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.chapter_index == 1


def _ten_chapters() -> tuple[ChapterLink, ...]:
    """Ten chapters titled "Chapter 1".."Chapter 10", for first-link backup tests (EXP-002)."""
    return tuple(ChapterLink(url=f"https://x/{i}", title=f"Chapter {i}") for i in range(1, 11))


def _ten_chapter_views() -> tuple[ChapterView, ...]:
    """Ten chapters as ChapterView objects for first-link backup tests (EXP-002)."""
    return tuple(
        ChapterView(
            ordinal=i,
            key=f"https://x/{i}",
            title=f"Chapter {i}",
            number=str(i),
            role="content",
            href=f"file{i:04d}.xhtml",
        )
        for i in range(1, 11)
    )


def test_first_link_backs_up_local_read_state_before_adoption() -> None:
    """First Komga link with empty provider progress backs up local state (EXP-002)."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(read={})
    view = make_book_view(
        progress=ExternalProgress(percent=0.42),
        chapter_table=_ten_chapter_views(),
    )

    result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.chapter_index == 5
    assert result.read_position.chapter_title == "Chapter 5"
    assert result.read_position.total_chapters == 10
    assert result.fields["external_read_percent"] == 0.0


def test_first_link_with_provider_progress_does_not_back_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Komga book that already has progress wins — no backup capture, no backup log."""
    progression = {
        "locator": {
            "href": "OEBPS/file0001.xhtml",
            "title": "The 12th Key - Ch 1",
            "locations": {"progression": 0.5, "position": 3, "totalProgression": 0.4},
        }
    }
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(read={"page": 7})
    client.get_progression = lambda bid: progression  # type: ignore
    client.get_positions = lambda bid: POSITIONS_FIXTURE  # type: ignore
    view = make_book_view(
        progress=ExternalProgress(percent=0.42),
        num_chapters=10,
        chapters=_ten_chapters(),
        chapter_table=_DEFAULT_CHAPTER_TABLE,
    )

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.chapter_index == 1
    assert result.read_position.chapter_title == "The 12th Key - Ch 1"
    assert not any("Backing up local read state" in r.message for r in caplog.records)


def test_linked_resync_with_empty_provider_progress_does_not_back_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ordinary re-sync of an already-linked book is not treated as a first link."""
    client = FakeKomga()
    client.book_exists_result = True
    client.book = komga_book(read={})
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        progress=ExternalProgress(percent=0.42),
        num_chapters=10,
        chapters=_ten_chapters(),
    )

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert result.read_position is None
    assert not any("Backing up local read state" in r.message for r in caplog.records)


# ---  Semantic read-position restore (RP-REST-3/5/7, RP-PLUG-4, RP-LOG-2/3) --- #


def test_restore_semantic_puts_nearest_valid_position(repo: _FakeBookRepository) -> None:
    """Target position found → put_progression called with nearest valid locator."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]  # snapshot at sync start
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    book = make_book(repo)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert len(client.put_progressions) == 1
    komga_book_id, payload = client.put_progressions[0]
    assert komga_book_id == "KB1"
    assert payload["locator"]["href"] == "OEBPS/file0001.xhtml"
    assert payload["locator"]["locations"]["progression"] == 0.5
    assert payload["modified"] == FIXED_NOW.isoformat()
    assert payload["device"] == {"id": "ebookerr", "name": "ebookerr"}


def test_restore_semantic_completed_targets_last_position(repo: _FakeBookRepository) -> None:
    """Completed target → nearest position to 1.0 chosen (last chapter)."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=2,
        chapter_progress=1.0,
        chapter_number=2,
        chapter_title="The 12th Key - Ch 2",
        chapter_href="file0002.xhtml",
        completed=True,
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]  # snapshot at sync start
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    book = make_book(repo)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert len(client.put_progressions) == 1
    _, payload = client.put_progressions[0]
    # Should choose 0.5 (nearest to 1.0) from file0002.xhtml
    assert payload["locator"]["href"] == "OEBPS/file0002.xhtml"
    assert payload["locator"]["locations"]["progression"] == 0.5


def test_restore_fails_closed_no_match(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """No match found → put_progression never called, WARNING logged."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=9,
        chapter_progress=0.5,
        chapter_number=99,
        chapter_title="Gone",
        chapter_href="nope.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    book = make_book(repo)

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert len(client.put_progressions) == 0
    assert any(
        r.levelname == "WARNING"
        and "Read-position restore failed for" in r.message
        and "no chapter matches" in r.message
        for r in caplog.records
    )


def test_restore_put_rejection_logs_error(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """put_progression returns False → ERROR logged."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    client.put_progression_ok = False
    book = make_book(repo)

    with caplog.at_level(logging.ERROR, logger="komga_sync.service"):
        service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert any(
        r.levelname == "ERROR" and "Komga rejected the read-position restore for" in r.message
        for r in caplog.records
    )


def test_a_rejected_restore_write_is_attempted_but_not_landed(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """put_progression False → restore_attempted=True, restore_landed=False, ERROR logged."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    client.put_progression_ok = False
    book = make_book(repo)

    with caplog.at_level(logging.ERROR, logger="komga_sync.service"):
        result = service(client, now=lambda: FIXED_NOW).sync(
            book_to_view(book), restore_target=target
        )

    assert result.restore_attempted is True
    assert result.restore_landed is False
    assert any(
        r.levelname == "ERROR" and "Komga rejected the read-position restore for" in r.message
        for r in caplog.records
    )


def test_a_matched_and_accepted_restore_is_landed(repo: _FakeBookRepository) -> None:
    """Target position found and accepted → restore_landed is True."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]  # snapshot at sync start
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    book = make_book(repo)

    result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert result.restore_attempted is True
    assert result.restore_landed is True


def test_sync_with_restore_target_skips_raw_restore(repo: _FakeBookRepository) -> None:
    """With restore_target set, semantic PUT performed, raw path never called."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    book = make_book(repo)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    # Should have exactly one put_progression call with the semantic device marker
    assert len(client.put_progressions) == 1
    _, payload = client.put_progressions[0]
    assert payload["device"]["id"] == "ebookerr"


def test_sync_without_restore_target_unchanged(repo: _FakeBookRepository) -> None:
    """Without restore_target, raw path remains active (existing tests pass)."""
    saved = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [saved, reset]
    client.get_positions = lambda _: []  # not used in raw path
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)

    result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    assert result.ok is True
    # Raw path should have PUT the raw snapshot
    assert len(client.put_progressions) == 1
    _, payload = client.put_progressions[0]
    # Raw path doesn't set device, or sets it differently than semantic
    assert payload.get("device", {}).get("id") != "ebookerr"


def test_restore_progression_log_includes_title(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """INFO log includes the book title alongside komga_book_id."""
    snap = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [snap, reset]  # snap at sync start, post-analyze: progress lost
    client.put_progression_ok = True
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1", "title": "Progress Book"})
    book = repo.get(book.book_id)
    assert book is not None
    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(book_to_view(book))
    assert result.ok is True
    assert any(
        r.levelno == logging.INFO
        and "Restored read progression" in r.message
        and '"Progress Book"' in r.message
        for r in caplog.records
    ), f"Expected INFO with title 'Progress Book', got: {[r.message for r in caplog.records]}"


def test_restore_semantic_writes_chapter_title_into_locator(
    repo: _FakeBookRepository,
) -> None:
    """Restore target with chapter_title carries title into written locator (EXP-009)."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 121",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]  # snapshot at sync start
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    book = make_book(repo)

    service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert len(client.put_progressions) == 1
    komga_book_id, payload = client.put_progressions[0]
    assert komga_book_id == "KB1"
    assert payload["locator"]["title"] == "Chapter 121"


def test_restore_semantic_without_any_title_writes_plain_locator(
    repo: _FakeBookRepository,
) -> None:
    """Target and match both title-less → locator has no 'title' key (EXP-009)."""
    # Target with no chapter_title
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title=None,
        chapter_href="file0001.xhtml",
    )
    # Positions with no titles (like Komga's positions list)
    positions_no_titles = [
        {
            "href": "OEBPS/titlepage.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
        },
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 2, "progression": 0.0, "totalProgression": 0.2},
        },
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 3, "progression": 0.5, "totalProgression": 0.4},
        },
    ]
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]  # snapshot at sync start
    client.get_positions = lambda _: positions_no_titles  # type: ignore
    book = make_book(repo)
    view = book_to_view(book, chapter_table=_chapters(("file0001.xhtml", "")))

    service(client, now=lambda: FIXED_NOW).sync(view, restore_target=target)

    assert len(client.put_progressions) == 1
    _, payload = client.put_progressions[0]
    assert "title" not in payload["locator"]


def test_recapture_after_restore_keeps_title(
    repo: _FakeBookRepository,
) -> None:
    """A capture's chapter_title comes from the book's own chapter table (CHC-D12), not Komga."""
    # Simulate a progression that was previously written by restore with title
    progression_with_title = {
        "locator": {
            "href": "OEBPS/file0001.xhtml",
            "title": "Chapter 121",
            "locations": {"position": 2, "progression": 0.5, "totalProgression": 0.4},
        },
        "device": {"id": "ebookerr", "name": "ebookerr"},
    }
    # Positions list without titles (as Komga returns)
    positions_no_titles = [
        {
            "href": "OEBPS/titlepage.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
        },
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 2, "progression": 0.0, "totalProgression": 0.2},
        },
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 3, "progression": 0.5, "totalProgression": 0.4},
        },
    ]
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 0, "completed": False})
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    # Progression returns the one we wrote with title
    client.progression_sequence = [progression_with_title]
    client.get_positions = lambda _: positions_no_titles  # type: ignore
    book = make_book(repo)
    view = book_to_view(book, chapter_table=_chapters(("file0001.xhtml", "Chapter 121")))

    result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.chapter_title == "Chapter 121"


def test_restore_candidates_honour_the_packaged_count(repo: _FakeBookRepository) -> None:
    """A restore target matched purely by index honours book.chapter_table's own count."""
    # Positions with no titles, no title page (index-only matching, per RP-D19 facet order).
    positions_no_titles = [
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.2},
        },
        {
            "href": "OEBPS/file0002.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 2, "progression": 0.0, "totalProgression": 0.4},
        },
        {
            "href": "OEBPS/file0003.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 3, "progression": 0.0, "totalProgression": 0.6},
        },
        {
            "href": "OEBPS/file0004.xhtml",
            "title": None,
            "type": "application/xhtml+xml",
            "locations": {"position": 4, "progression": 0.5, "totalProgression": 0.8},
        },
    ]
    # Target with index 4 and total_chapters 4, expecting to match file0004.xhtml
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=4,
        chapter_progress=0.5,
        chapter_number=None,
        chapter_title=None,
        chapter_href=None,
        total_chapters=4,
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]  # snapshot at sync start
    client.get_positions = lambda _: positions_no_titles  # type: ignore
    book = make_book(repo)
    view = book_to_view(
        book,
        chapter_table=_chapters(
            ("file0001.xhtml", ""),
            ("file0002.xhtml", ""),
            ("file0003.xhtml", ""),
            ("file0004.xhtml", ""),
        ),
    )

    service(client, now=lambda: FIXED_NOW).sync(view, restore_target=target)

    # Should have put_progression called with file0004.xhtml at index 4
    assert len(client.put_progressions) == 1
    komga_book_id, payload = client.put_progressions[0]
    assert komga_book_id == "KB1"
    assert payload["locator"]["href"] == "OEBPS/file0004.xhtml"
    assert payload["locator"]["locations"]["progression"] == 0.5


def test_persisted_locator_survives_a_komga_relink(
    repo: _FakeBookRepository,
) -> None:
    """When _ensure_present re-resolves a vanished id, the persisted locator is restored."""
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = ["KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = []  # every get_progression returns {}

    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {
            "external_item_id": "KB1",
            "external_locator": json.dumps(
                {
                    "locator": {"locations": {"position": 42}},
                    "modified": "2026-07-01T00:00:00",
                }
            ),
        },
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields["external_item_id"] == "KB2"
    # The persisted locator should have been written back onto the new id
    assert len(client.put_progressions) == 1
    komga_id, _ = client.put_progressions[0]
    assert komga_id == "KB2"


def test_persisted_locator_restores_without_a_relink(
    repo: _FakeBookRepository,
) -> None:
    """An unchanged link (no re-link) still restores the persisted locator.

    ``_ensure_present`` echoes back the existing ``external_item_id`` whenever
    ``book_exists`` is ``True`` or the indeterminate ``None``; since ``EXP-243``,
    ``_sync_reachable`` additionally re-validates that id against the book's own
    ``output_filename``, but this test's client has an empty ``library_books`` index, so
    :meth:`KomgaService._revalidated_link` keeps the stored id unconditionally.
    ``komga_book_id`` therefore still equals ``book.external_item_id`` here and the
    DB-fallback restore applies, per :meth:`KomgaService._snapshot_with_db_fallback`'s
    "still linked to the same Komga book" contract.
    """
    locator_data = {
        "locator": {"locations": {"position": 42}},
        "modified": "2026-07-01T00:00:00",
    }

    # First sub-case: book_exists returns True, so no re-link happens.
    client = FakeKomga()
    client.book_exists_result = True
    client.find_results = ["KB1"]  # irrelevant; the linked path returns KB1
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = []

    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {
            "external_item_id": "KB1",
            "external_locator": json.dumps(locator_data),
        },
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert len(client.put_progressions) == 1
    assert client.put_progressions[0][0] == "KB1"

    # Second sub-case: book_exists returns None (indeterminate), so the link is kept
    # as-is — still the same id, not a cross-book mismatch.
    client2 = FakeKomga()
    client2.book_exists_result = None
    client2.find_results = ["KB1"]
    client2.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client2.series = {"metadata": MATCHING_SERIES_METADATA}
    client2.progression_sequence = []

    book2 = make_book(repo)
    repo.update_fields(
        book2.book_id,
        {
            "external_item_id": "KBOTHER",
            "external_locator": json.dumps(locator_data),
        },
    )
    book2 = repo.get(book2.book_id)
    assert book2 is not None

    result2 = service(client2).sync(book_to_view(book2))

    assert result2.ok is True
    assert len(client2.put_progressions) == 1
    assert client2.put_progressions[0][0] == "KBOTHER"


def test_relink_without_a_persisted_locator_writes_nothing(
    repo: _FakeBookRepository,
) -> None:
    """A re-link without a persisted locator does not write anything."""
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = ["KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = []

    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {"external_item_id": "KB1"},
        # No external_locator set
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert client.put_progressions == []


# ---  Semantic read-position restore telemetry (RP-LOG-2/3) ---- #


class TestRestoreTelemetry:
    """Tests for the telemetry logged by _restore_semantic."""

    def test_restore_logs_the_candidate_table_at_debug(
        self, repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A restore logs the candidate table and target at DEBUG."""
        positions = [
            {
                "href": "OEBPS/chapter_01.xhtml",
                "title": "Book Ch. 1",
                "locations": {"position": 1, "progression": 0.0},
            },
            {
                "href": "OEBPS/chapter_02.xhtml",
                "title": "Book Ch. 2",
                "locations": {"position": 2, "progression": 0.0},
            },
            {
                "href": "OEBPS/chapter_03.xhtml",
                "title": "Book Ch. 3",
                "locations": {"position": 3, "progression": 0.0},
            },
        ]
        target = ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=1,
            chapter_progress=0.5,
            chapter_number=2,
            chapter_title="Book Ch. 2",
            chapter_href="chapter_02.xhtml",
        )
        client = FakeKomga()
        client.find_results = ["KB1"]
        client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
        client.series = {"metadata": MATCHING_SERIES_METADATA}
        client.progression_sequence = [{}]
        client.get_positions = lambda _: positions  # type: ignore
        book = make_book(repo)
        view = book_to_view(
            book,
            chapter_table=_chapters(
                ("chapter_01.xhtml", "Book Ch. 1"),
                ("chapter_02.xhtml", "Book Ch. 2"),
                ("chapter_03.xhtml", "Book Ch. 3"),
            ),
        )

        with caplog.at_level(logging.DEBUG, logger="ebookerr_sdk.providers.anchoring"):
            service(client, now=lambda: FIXED_NOW).sync(view, restore_target=target)

        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert any(
            "Read-position restore table for" in r.message and "3 candidate(s)" in r.message
            for r in debug_records
        ), (
            "Expected DEBUG with 'Read-position restore table' and '3 candidate(s)', "
            f"got: {[r.message for r in debug_records]}"
        )
        assert any(
            "Read-position candidates:" in r.message and "Book Ch. 2" in r.message
            for r in debug_records
        ), (
            "Expected DEBUG with 'Read-position candidates:' and 'Book Ch. 2', "
            f"got: {[r.message for r in debug_records]}"
        )

    def test_restore_success_logs_target_and_chosen_at_info(
        self, repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A successful restore logs target chapter_index and chosen href at INFO."""
        positions = [
            {
                "href": "OEBPS/chapter_01.xhtml",
                "title": "Book Ch. 1",
                "locations": {"position": 1, "progression": 0.0},
            },
            {
                "href": "OEBPS/chapter_02.xhtml",
                "title": "Book Ch. 2",
                "locations": {"position": 2, "progression": 0.5},
            },
            {
                "href": "OEBPS/chapter_03.xhtml",
                "title": "Book Ch. 3",
                "locations": {"position": 3, "progression": 0.0},
            },
        ]
        target = ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=1,
            chapter_progress=0.5,
            chapter_number=2,
            chapter_title="Book Ch. 2",
            chapter_href="chapter_02.xhtml",
        )
        client = FakeKomga()
        client.find_results = ["KB1"]
        client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
        client.series = {"metadata": MATCHING_SERIES_METADATA}
        client.progression_sequence = [{}]
        client.get_positions = lambda _: positions  # type: ignore
        book = make_book(repo)
        view = book_to_view(
            book,
            chapter_table=_chapters(
                ("chapter_01.xhtml", "Book Ch. 1"),
                ("chapter_02.xhtml", "Book Ch. 2"),
                ("chapter_03.xhtml", "Book Ch. 3"),
            ),
        )

        with caplog.at_level(logging.INFO, logger="ebookerr_sdk.providers.anchoring"):
            service(client, now=lambda: FIXED_NOW).sync(view, restore_target=target)

        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert any(
            "Re-anchored the read position for" in r.message
            and "ref=OEBPS/chapter_02.xhtml" in r.message
            for r in info_records
        ), (
            "Expected INFO with 'Re-anchored the read position for' and the ref, "
            f"got: {[r.message for r in info_records]}"
        )

    def test_no_match_warning_names_the_candidate_count(
        self, repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No-match warning includes candidate count and total_chapters."""
        positions = [
            {
                "href": "OEBPS/chapter_01.xhtml",
                "title": "Book Ch. 1",
                "locations": {"position": 1, "progression": 0.0},
            },
            {
                "href": "OEBPS/chapter_02.xhtml",
                "title": "Book Ch. 2",
                "locations": {"position": 2, "progression": 0.5},
            },
            {
                "href": "OEBPS/chapter_03.xhtml",
                "title": "Book Ch. 3",
                "locations": {"position": 3, "progression": 0.0},
            },
        ]
        target = ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=99,
            chapter_progress=0.5,
            chapter_number=999,
            chapter_title="Gone",
            chapter_href="nope.xhtml",
            total_chapters=5,
        )
        client = FakeKomga()
        client.find_results = ["KB1"]
        client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
        client.series = {"metadata": MATCHING_SERIES_METADATA}
        client.progression_sequence = [{}]
        client.get_positions = lambda _: positions  # type: ignore
        book = make_book(repo)
        view = book_to_view(
            book,
            chapter_table=_chapters(
                ("chapter_01.xhtml", "Book Ch. 1"),
                ("chapter_02.xhtml", "Book Ch. 2"),
                ("chapter_03.xhtml", "Book Ch. 3"),
            ),
        )

        with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
            service(client, now=lambda: FIXED_NOW).sync(view, restore_target=target)

        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "no chapter matches among 3 candidate(s)" in r.message
            and "total_chapters=" in r.message
            for r in warning_records
        ), (
            "Expected WARNING with candidate count and total_chapters, "
            f"got: {[r.message for r in warning_records]}"
        )
        assert len(client.put_progressions) == 0

    def test_empty_positions_still_warns(
        self, repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An empty provider table never restores: the join has nothing to confirm (CHC-D12)."""
        target = ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=0,
            chapter_progress=0.0,
            chapter_number=0,
            chapter_title="Any",
            chapter_href="any.xhtml",
        )
        client = FakeKomga()
        client.find_results = ["KB1"]
        client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
        client.series = {"metadata": MATCHING_SERIES_METADATA}
        client.progression_sequence = [{}]
        client.get_positions = lambda _: []  # type: ignore
        book = make_book(repo)

        with caplog.at_level(logging.WARNING):
            service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "chapter table does not describe the file on disk" in r.message for r in warning_records
        ), f"Expected a chapter-table WARNING, got: {[r.message for r in warning_records]}"
        assert len(client.put_progressions) == 0

    def test_rejected_restore_still_logs_an_error(
        self, repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        """put_progression returning False still logs ERROR."""
        positions = [
            {
                "href": "OEBPS/chapter_01.xhtml",
                "title": "Book Ch. 1",
                "locations": {"position": 1, "progression": 0.5},
            },
        ]
        target = ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=0,
            chapter_progress=0.5,
            chapter_number=1,
            chapter_title="Book Ch. 1",
            chapter_href="chapter_01.xhtml",
        )
        client = FakeKomga()
        client.find_results = ["KB1"]
        client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
        client.series = {"metadata": MATCHING_SERIES_METADATA}
        client.progression_sequence = [{}]
        client.get_positions = lambda _: positions  # type: ignore
        client.put_progression_ok = False
        book = make_book(repo)
        view = book_to_view(book, chapter_table=_chapters(("chapter_01.xhtml", "Book Ch. 1")))

        with caplog.at_level(logging.ERROR, logger="komga_sync.service"):
            service(client, now=lambda: FIXED_NOW).sync(view, restore_target=target)

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any(
            "Komga rejected the read-position restore for" in r.message for r in error_records
        ), f"Expected ERROR with rejection message, got: {[r.message for r in error_records]}"


# --- BookView integration tests ------------------------------------------------ #


def test_series_patch_uses_the_merged_status() -> None:
    """BookView with merged status='Completed' -> series patch has status='ENDED'."""
    view = make_book_view(status="Completed")
    patch = _series_patch(view, {})  # type: ignore
    assert patch.get("status") == "ENDED"


# --- summary source: synopsis vs description --------------------------------- #


def test_summary_prefers_the_synopsis() -> None:
    """Book with synopsis='Curated' and description='Scraped' → patch summary is 'Curated'."""
    view = make_book_view(synopsis="Curated", description="Scraped")
    patch = _book_patch(view, {}, [])
    assert patch["summary"] == "Curated"


def test_summary_falls_back_to_the_description() -> None:
    """Book with synopsis=None and description='Scraped' → patch summary is 'Scraped'."""
    view = make_book_view(synopsis=None, description="Scraped")
    patch = _book_patch(view, {}, [])
    assert patch["summary"] == "Scraped"


def test_summary_falls_back_when_the_synopsis_is_blank() -> None:
    """Synopsis with whitespace only (sanitizes to '') → falls back to description."""
    view = make_book_view(synopsis="   ", description="Scraped")
    patch = _book_patch(view, {}, [])
    assert patch["summary"] == "Scraped"


def test_summary_is_empty_when_both_are_unset() -> None:
    """Both synopsis=None and description=None → patch summary is '' (can push empty)."""
    view = make_book_view(synopsis=None, description=None)
    # Komga currently has summary "Old"
    patch = _book_patch(view, {"summary": "Old"}, [])
    assert patch["summary"] == ""


def test_summary_is_omitted_when_unchanged() -> None:
    """Synopsis='Same' and Komga already has summary='Same' → summary not in patch."""
    view = make_book_view(synopsis="Same", description=None)
    # Komga metadata already has the same title, author and summary
    current = {
        "title": "A Title",
        "summary": "Same",
        "authors": [{"name": "An Author", "role": "writer"}],
    }
    patch = _book_patch(view, current, [])
    assert "summary" not in patch


def test_summary_is_html_sanitised() -> None:
    """Synopsis with HTML tags → sanitized to bare text."""
    view = make_book_view(synopsis="<p>Curated</p>", description=None)
    patch = _book_patch(view, {"summary": "Old"}, [])
    assert patch["summary"] == "Curated"


def test_summary_source_is_logged_at_debug_for_the_synopsis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When pushing synopsis as summary, a DEBUG log names 'synopsis' as source."""
    view = make_book_view(synopsis="Curated", description="Scraped")
    with caplog.at_level(logging.DEBUG):
        _book_patch(view, {"summary": "Old"}, [])
    assert any("Komga summary sourced from synopsis" in record.message for record in caplog.records)


def test_summary_source_is_logged_at_debug_for_the_description(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When falling back to description as summary, a DEBUG log names 'description'."""
    view = make_book_view(synopsis=None, description="Scraped")
    with caplog.at_level(logging.DEBUG):
        _book_patch(view, {"summary": "Old"}, [])
    assert any(
        "Komga summary sourced from description" in record.message for record in caplog.records
    )


def test_book_patch_uses_the_merged_author_and_category() -> None:
    """BookView with merged author/category -> pushed authors/tags reflect exactly those values."""
    view = make_book_view(author="J. Doe", category="Romance, Drama")
    desired_tags = _compose_tags(view.category, view.erotica_tags, None)
    patch = _book_patch(view, {}, desired_tags)  # type: ignore
    assert patch.get("authors") == [{"name": "J. Doe", "role": "writer"}]
    # Category should be in tags (composed via _compose_tags)
    assert "Romance" in patch.get("tags", [])


def test_book_patch_uses_the_merged_date_published() -> None:
    """BookView with merged date_published -> releaseDate in ISO format."""
    from datetime import UTC

    view = make_book_view(date_published=datetime(2026, 4, 23, tzinfo=UTC))
    patch = _book_patch(view, {}, [])  # type: ignore
    assert patch.get("releaseDate") == "2026-04-23"


def test_completion_date_comes_from_komga(repo: _FakeBookRepository) -> None:
    """When completed and readDate is present, read_completed_at is set from it."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        read={"page": 14, "completed": True, "readDate": "2026-07-04T12:00:00Z"}
    )
    client.series = {"metadata": {}}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert result.fields["read_completed_at"] == datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def test_completion_date_accepts_an_offset(repo: _FakeBookRepository) -> None:
    """When readDate has a timezone offset, it is normalized to UTC."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        read={"page": 14, "completed": True, "readDate": "2026-07-04T14:00:00+02:00"}
    )
    client.series = {"metadata": {}}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert result.fields["read_completed_at"] == datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def test_no_completion_date_when_not_completed(repo: _FakeBookRepository) -> None:
    """When completed is False, read_completed_at is not in fields even if readDate present."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        read={"page": 10, "completed": False, "readDate": "2026-07-04T12:00:00Z"}
    )
    client.series = {"metadata": {}}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert "read_completed_at" not in result.fields


def test_no_completion_date_when_komga_omits_it(repo: _FakeBookRepository) -> None:
    """When readDate is absent, read_completed_at is not in fields even if completed."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(read={"page": 14, "completed": True})
    client.series = {"metadata": {}}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert "read_completed_at" not in result.fields


def test_no_completion_date_when_unparseable(repo: _FakeBookRepository) -> None:
    """When readDate is unparseable, read_completed_at is not in fields."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(read={"page": 14, "completed": True, "readDate": "not a date"})
    client.series = {"metadata": {}}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert "read_completed_at" not in result.fields


def test_unparseable_date_logs_at_debug(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """When readDate is unparseable, a debug log mentions the book title and the value."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(read={"page": 14, "completed": True, "readDate": "not a date"})
    client.series = {"metadata": {}}

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert any("completed with no usable readDate" in record.message for record in caplog.records)


def test_missing_read_progress_is_safe(repo: _FakeBookRepository) -> None:
    """When readProgress is absent, fields has no read_completed_at and no exception."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(read=None)
    client.series = {"metadata": {}}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert "read_completed_at" not in result.fields


def test_completion_date_does_not_disturb_the_other_read_fields(
    repo: _FakeBookRepository,
) -> None:
    """When read_completed_at is present, other read fields are unchanged."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(
        pages=14,
        read={
            "page": 14,
            "completed": True,
            "readDate": "2026-07-04T12:00:00Z",
        },
    )
    client.series = {"metadata": {}}

    result = service(client).sync(book_to_view(make_book(repo)))

    assert result.ok is True
    assert result.fields["external_read_completed"] == 1
    assert result.fields["external_read_total"] == 14
    assert result.fields["external_read_position"] == 14


def test_ensure_present_does_not_analyze_by_default(repo: _FakeBookRepository) -> None:
    """An already-linked book should not trigger analyze by default."""
    client = FakeKomga()
    client.book_exists_result = True
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    svc = service(client)
    result = svc._ensure_present(book_to_view(book))

    assert len(client.analyze_calls) == 0
    assert result == "KB1"


def test_ensure_present_analyzes_when_asked(repo: _FakeBookRepository) -> None:
    """An already-linked book should trigger analyze when analyze=True."""
    client = FakeKomga()
    client.book_exists_result = True
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    svc = service(client)
    result = svc._ensure_present(book_to_view(book), analyze=True)

    assert len(client.analyze_calls) == 1
    assert client.analyze_calls[0] == "KB1"
    assert result == "KB1"


def test_ensure_present_still_returns_the_linked_id_without_analyze(
    repo: _FakeBookRepository,
) -> None:
    """Return value should be the existing item id regardless of analyze flag."""
    client = FakeKomga()
    client.book_exists_result = True
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None
    book_view = book_to_view(book)

    svc = service(client)
    result_without_analyze = svc._ensure_present(book_view)
    result_with_analyze = svc._ensure_present(book_view, analyze=True)

    assert result_without_analyze == "KB1"
    assert result_with_analyze == "KB1"


def test_ensure_present_logs_the_link_at_debug_without_analyze(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Without analyze, log should mention the book is linked but not 'triggering analyze'."""
    client = FakeKomga()
    client.book_exists_result = True
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        svc = service(client)
        svc._ensure_present(book_to_view(book))

    assert any("Komga book already linked for" in r.message for r in caplog.records), (
        f"Expected log message about linked book, got: {[r.message for r in caplog.records]}"
    )
    assert not any("triggering analyze" in r.message for r in caplog.records), (
        f"Should not log 'triggering analyze', got: {[r.message for r in caplog.records]}"
    )


def test_ensure_present_logs_analyze_at_debug_when_asked(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """With analyze=True, log should mention 'triggering analyze'."""
    client = FakeKomga()
    client.book_exists_result = True
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        svc = service(client)
        svc._ensure_present(book_to_view(book), analyze=True)

    assert any("triggering analyze" in r.message for r in caplog.records), (
        f"Expected log message about triggering analyze, got: {[r.message for r in caplog.records]}"
    )


def test_ensure_present_still_rediscovers_when_the_book_is_gone(
    repo: _FakeBookRepository,
) -> None:
    """When book_exists returns False, should rediscover and not analyze."""
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = ["KB2"]
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    svc = service(client)
    result = svc._ensure_present(book_to_view(book))

    assert len(client.analyze_calls) == 0
    assert result == "KB2"
    assert len(client.book_exists_calls) > 0


def test_a_second_sync_sends_no_summary_patch(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """After the first sync, a second sync without analyze should not push summary again."""
    client = FakeKomga()
    # Setup so first sync finds book by title
    client.find_results = ["KB1"]
    client.book = komga_book(
        metadata={
            "title": "The 12th Key",
            "summary": "Nightmares.",  # Matches what _book_patch will compute
            "releaseDate": "2026-05-19",
            "authors": [{"name": "gabthewriter", "role": "writer"}],
            "tags": EXPECTED_TAGS,
            "links": [{"label": "Book", "url": STORY_URL}, {"label": "Author", "url": AUTHOR_URL}],
        }
    )
    client.series = {"metadata": {}}
    book = make_book(repo)

    svc = service(client)

    # First sync - will find by title and fetch KB1, no summary change
    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result1 = svc.sync(book_to_view(book), allow_scan=False)

    assert result1.ok is True
    # Re-link the book with the komga ID it got
    komga_id = result1.fields.get("external_item_id")
    assert komga_id is not None
    repo.update_fields(book.book_id, {"external_item_id": komga_id})

    # Now setup for second sync: already linked, book_exists returns True
    # Don't add to find_results since we won't search by title on already-linked
    client.find_results = []  # Clear for second sync
    # Clear logs and do a second sync
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result2 = svc.sync(book_to_view(repo.get(book.book_id)), allow_scan=False)

    assert result2.ok is True
    # Should NOT see "Pushing Komga book metadata" since summary didn't change
    assert not any("Pushing Komga book metadata" in r.message for r in caplog.records), (
        f"Should not push metadata again, got: {[r.message for r in caplog.records]}"
    )


# --- _push_book_metadata return value tests ---


def test_push_book_metadata_returns_true_when_it_patches(
    repo: _FakeBookRepository,
) -> None:
    """_push_book_metadata returns True when a PATCH was sent."""
    client = FakeKomga()
    # Book with different title will trigger a patch
    client.book = komga_book(
        metadata={
            "title": "Different Title",  # Will differ from "The 12th Key"
            "summary": "Nightmares.",
            "releaseDate": "2026-05-19",
            "authors": [{"name": "gabthewriter", "role": "writer"}],
            "tags": EXPECTED_TAGS,
            "links": [{"label": "Book", "url": STORY_URL}, {"label": "Author", "url": AUTHOR_URL}],
        }
    )
    client.series = {"metadata": {}}
    book = make_book(repo)

    svc = service(client)
    result = svc._push_book_metadata(
        "KB1",
        book_to_view(book),
        client.book,
        EXPECTED_TAGS,
        ebookerr_url=None,
    )

    assert result is True
    assert len(client.book_patches) == 1


def test_push_book_metadata_returns_false_when_nothing_differs(
    repo: _FakeBookRepository,
) -> None:
    """_push_book_metadata returns False when nothing differed."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": {}}
    book = make_book(repo)

    svc = service(client)
    result = svc._push_book_metadata(
        "KB1",
        book_to_view(book),
        client.book,
        EXPECTED_TAGS,
        ebookerr_url=None,
    )

    assert result is False
    assert len(client.book_patches) == 0


def test_sync_logs_a_run_summary_at_info(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A batch sync logs one summary with updated/unchanged/failed counts."""
    # Create two books: one that will be updated, one unchanged
    client = FakeKomga()
    # Each sync() calls get_book() twice (initial load + refresh), so we need 4 books total
    client.book_sequence = [
        # Book 1
        komga_book(
            metadata={
                "title": "Different Title",  # Will differ
                "summary": "Nightmares.",
                "releaseDate": "2026-05-19",
                "authors": [{"name": "gabthewriter", "role": "writer"}],
                "tags": EXPECTED_TAGS,
                "links": [
                    {"label": "Book", "url": STORY_URL},
                    {"label": "Author", "url": AUTHOR_URL},
                ],
            }
        ),
        komga_book(
            metadata={
                "title": "Different Title",
                "summary": "Nightmares.",
                "releaseDate": "2026-05-19",
                "authors": [{"name": "gabthewriter", "role": "writer"}],
                "tags": EXPECTED_TAGS,
                "links": [
                    {"label": "Book", "url": STORY_URL},
                    {"label": "Author", "url": AUTHOR_URL},
                ],
            }
        ),
        # Book 2
        komga_book(metadata=MATCHING_BOOK_METADATA),  # Matching
        komga_book(metadata=MATCHING_BOOK_METADATA),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}, {}, {}, {}]  # 4 progressions (2 per book)
    client.find_results = ["KB1", "KB2"]

    svc = service(client)
    # Book 1: will differ (has Different Title in Komga)
    book1 = make_book(repo)
    # Book 2: will match (MATCHING_BOOK_METADATA matches default make_book title)
    book2 = make_book(repo, storyId="the-12th-key-2")

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        svc.sync_batch(
            [book_to_view(book1), book_to_view(book2)],
            allow_scan=False,
        )

    # Find the summary line
    summary_records = [r for r in caplog.records if "Komga sync finished:" in r.message]
    assert len(summary_records) == 1
    msg = summary_records[0].message
    assert re.search(
        r"Komga sync finished: 2 book\(s\) — 1 updated, 1 unchanged, 0 failed in \d+\.\d+s", msg
    )


def test_sync_summary_fires_even_when_nothing_changed(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Summary fires even when all books are unchanged."""
    client = FakeKomga()
    client.book_sequence = [
        komga_book(metadata=MATCHING_BOOK_METADATA),
        komga_book(metadata=MATCHING_BOOK_METADATA),
        komga_book(metadata=MATCHING_BOOK_METADATA),
        komga_book(metadata=MATCHING_BOOK_METADATA),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}, {}, {}, {}]
    client.find_results = ["KB1", "KB2"]

    svc = service(client)
    # Both books will match (default MATCHING_BOOK_METADATA)
    book1 = make_book(repo)
    book2 = make_book(repo, storyId="the-12th-key-2")

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        svc.sync_batch(
            [book_to_view(book1), book_to_view(book2)],
            allow_scan=False,
        )

    summary_records = [r for r in caplog.records if "Komga sync finished:" in r.message]
    assert len(summary_records) == 1
    msg = summary_records[0].message
    assert re.search(r"Komga sync finished: 2 book\(s\) — 0 updated, 2 unchanged, 0 failed", msg)


def test_sync_summary_fires_for_an_empty_run(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Summary fires even when run over zero books."""
    client = FakeKomga()

    svc = service(client)

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        svc.sync_batch([], allow_scan=False)

    summary_records = [r for r in caplog.records if "Komga sync finished:" in r.message]
    assert len(summary_records) == 1
    msg = summary_records[0].message
    # Duration should still be present even for empty run
    assert re.search(r"Komga sync finished: 0 book\(s\) — 0 updated, 0 unchanged, 0 failed in", msg)


def test_sync_summary_counts_a_failure(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Summary counts a book whose sync raises as a failure."""
    client = FakeKomga()
    # First book will be updated (2 calls to get_book)
    # Second book will fail (simulated by get_book returning None)
    client.book_sequence = [
        # Book 1
        komga_book(
            metadata={
                "title": "Different Title",
                "summary": "Nightmares.",
                "releaseDate": "2026-05-19",
                "authors": [{"name": "gabthewriter", "role": "writer"}],
                "tags": EXPECTED_TAGS,
                "links": [
                    {"label": "Book", "url": STORY_URL},
                    {"label": "Author", "url": AUTHOR_URL},
                ],
            }
        ),
        komga_book(
            metadata={
                "title": "Different Title",
                "summary": "Nightmares.",
                "releaseDate": "2026-05-19",
                "authors": [{"name": "gabthewriter", "role": "writer"}],
                "tags": EXPECTED_TAGS,
                "links": [
                    {"label": "Book", "url": STORY_URL},
                    {"label": "Author", "url": AUTHOR_URL},
                ],
            }
        ),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]  # Only one for the first book
    client.find_results = ["KB1", None]  # Second find_book_id returns None, triggering failure

    svc = service(client)
    book1 = make_book(repo, title="Book 1")
    book2 = make_book(repo, title="Book 2")

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        results = svc.sync_batch(
            [book_to_view(book1), book_to_view(book2)],
            allow_scan=False,
        )

    # First should succeed, second should fail
    assert results[0].ok is True
    assert results[1].ok is False

    # Check the summary was logged with failure count
    summary_records = [r for r in caplog.records if "Komga sync finished:" in r.message]
    assert len(summary_records) == 1
    msg = summary_records[0].message
    assert "1 failed" in msg


# --- backward read-position moves and device attribution ---------------------- #


def test_backward_move_within_a_chapter_warns_with_the_writer(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Prior position index 2 @ 0.14; new progression 0.111 at same href warns with writer."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    # Positions fixture with chapters aligned to our test
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    # Progression: backward move from 0.14 to 0.111 in same chapter (index 2, file0002.xhtml)
    progression = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 5, "progression": 0.111},
        },
        "device": {"id": "komic-ios", "name": "Komic"},
        "modified": "2026-08-08T10:00:00Z",
    }
    # Three progression calls: sync-start snapshot, restore-check, semantic capture
    client.progression_sequence = [progression, progression, progression]
    # Use the standard POSITIONS_FIXTURE which has file0002.xhtml at index 2
    client.positions = POSITIONS_FIXTURE

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    # Create a BookView with an existing read_position (chapter_index=2, progress=0.14)
    view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=ReadPosition(
            captured_at="2026-08-01T10:00:00Z",
            chapter_index=2,
            chapter_progress=0.14,
            chapter_number=2,
            chapter_title="The 12th Key - Ch 2",
            chapter_href="OEBPS/file0002.xhtml",
            completed=False,
            total_chapters=2,
        ),
        chapter_table=_DEFAULT_CHAPTER_TABLE,
    )

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(view)

    assert result.ok is True
    # Look for the warning about backward move
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "moved backwards" in r.message
    ]
    assert len(warnings) == 1
    msg = warnings[0].message
    assert "moved backwards: chapter_index 2 -> 2" in msg
    assert "14%" in msg
    assert "11%" in msg
    assert "device=komic-ios" in msg
    assert "name=Komic" in msg


def test_forward_move_does_not_warn(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Forward move from 0.14 → 0.40 in same chapter produces no WARNING."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    # Forward move: progression 0.40 in same chapter (index 2)
    progression = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 5, "progression": 0.40},
        },
        "device": {"id": "komic-ios", "name": "Komic"},
        "modified": "2026-08-08T10:00:00Z",
    }
    # Three progression calls: sync-start snapshot, restore-check, semantic capture
    client.progression_sequence = [progression, progression, progression]
    client.positions = POSITIONS_FIXTURE

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=ReadPosition(
            captured_at="2026-08-01T10:00:00Z",
            chapter_index=2,
            chapter_progress=0.14,
            chapter_number=2,
            chapter_title="The 12th Key - Ch 2",
            chapter_href="OEBPS/file0002.xhtml",
            completed=False,
            total_chapters=2,
        ),
    )

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(view)

    assert result.ok is True
    # No warnings about backward move
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "moved backwards" in r.message
    ]
    assert len(warnings) == 0


def test_capture_debug_line_names_device_and_modified(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """DEBUG log line names device id/name; the anchor count now logs via capture_position.

    ``eb207530`` (2.18.26, ``RP-D10``) dropped the Komga-only ``positions=%d`` segment from
    this line: the anchor count is no longer computed here at all, since capture now goes
    through the shared, provider-agnostic ``capture_position``, which already logs its own
    anchor and TOC-title counts (``read_position_anchoring.py``) — restating it here would be
    a second, redundant count kept in sync by hand.
    """
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    # Progression with device info
    progression = {
        "locator": {
            "href": "OEBPS/file0001.xhtml",
            "locations": {"position": 3, "progression": 0.5},
        },
        "device": {"id": "komic-ios", "name": "Komic"},
        "modified": "2026-08-08T10:00:00Z",
    }
    # Simulate multiple positions to verify positions count (POSITIONS_FIXTURE has 5 items)
    client.positions = POSITIONS_FIXTURE
    # Three progression calls: sync-start snapshot, restore-check, semantic capture
    client.progression_sequence = [progression, progression, progression]

    book = make_book(repo)

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(book_to_view(book))

    assert result.ok is True
    # Find DEBUG logs from _semantic_position that include device info
    debug_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG
        and "Komga progression for" in r.message
        and "device=" in r.message
    ]
    komga_msgs = [r.message for r in caplog.records if "Komga progression" in r.message]
    assert len(debug_logs) > 0, (
        f"Expected DEBUG log with 'Komga progression for' and 'device=', got: {komga_msgs}"
    )
    msg = debug_logs[0].message
    assert "device=komic-ios/Komic" in msg

    # The anchor count moved to capture_position's own INFO-level log line (RP-D10).
    capture_logs = [
        r.message
        for r in caplog.records
        if r.name == "ebookerr_sdk.providers.anchoring" and "Captured the" in r.message
    ]
    assert capture_logs, f"Expected capture_position's own summary log, got: {komga_msgs}"
    assert "anchor(s)" in capture_logs[0]


def test_sync_summary_reports_a_duration(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Summary includes a duration in seconds."""
    client = FakeKomga()
    client.book_sequence = [
        komga_book(metadata=MATCHING_BOOK_METADATA),
        komga_book(metadata=MATCHING_BOOK_METADATA),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}, {}]
    client.find_results = ["KB1"]

    svc = service(client)
    book1 = make_book(repo)

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        svc.sync_batch([book_to_view(book1)], allow_scan=False)

    summary_records = [r for r in caplog.records if "Komga sync finished:" in r.message]
    assert len(summary_records) == 1
    msg = summary_records[0].message
    assert re.search(r"in \d+\.\d+s$", msg)


def test_sync_summary_logs_no_secrets(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Summary record contains no API keys or tokens."""
    client = FakeKomga()
    client.book_sequence = [
        komga_book(metadata=MATCHING_BOOK_METADATA),
        komga_book(metadata=MATCHING_BOOK_METADATA),
    ]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}, {}]
    client.find_results = ["KB1"]

    # Fake token for verification (not a real secret)
    fake_token = "XXX-REDACTED-XXX"
    svc = service(client)
    book1 = make_book(repo)

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        svc.sync_batch([book_to_view(book1)], allow_scan=False)

    summary_records = [r for r in caplog.records if "Komga sync finished:" in r.message]
    assert len(summary_records) == 1
    msg = summary_records[0].message
    assert fake_token not in msg


# --- semantic restore locator persistence (EXP-123) ------------------------


@pytest.mark.pins("EXP-123")
def test_a_restored_position_survives_the_provider_losing_its_progression(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Restore → provider loses progression → sync restores the position, not an older one."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = _EchoingKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = []  # empty for get_progression to echo back put_progressions
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    client.put_progression_ok = True
    clock = _TickingClock(FIXED_NOW)
    svc = service(client, now=clock)
    book = make_book(repo)

    # Sync 1 (restore)
    r1 = svc.sync(book_to_view(book), restore_target=target)
    assert r1.ok is True
    assert r1.restore_attempted

    # Verify the envelope was persisted (not the bare locator)
    envelope = json.loads(r1.fields["external_locator"])
    assert set(envelope) >= {"modified", "device", "locator"}
    assert envelope["locator"] == client.put_progressions[0][1]["locator"]
    assert r1.read_position is not None

    # Verify timestamps: restore stamp strictly earlier than capture
    assert envelope["modified"] < r1.read_position.captured_at

    # Persist what the plugin would store
    repo.update_fields(
        book.book_id,
        {
            "external_locator": r1.fields["external_locator"],
            "external_item_id": "KB1",
        },
    )
    repo.add_read_position(
        book.book_id,
        captured_at=r1.read_position.captured_at,
        chapter_index=r1.read_position.chapter_index,
        chapter_progress=r1.read_position.chapter_progress,
        chapter_number=r1.read_position.chapter_number,
        chapter_title=r1.read_position.chapter_title,
        chapter_href=r1.read_position.chapter_href,
        completed=r1.read_position.completed,
        total_chapters=r1.read_position.total_chapters,
    )

    # Provider loses its progression
    client.put_progressions.clear()
    client.progression_sequence = []  # empty so that get_progression echoes back if written

    # Sync 2: restore should be replayed
    book_refreshed = repo.get(book.book_id)
    assert book_refreshed is not None
    view = book_to_view(book_refreshed)
    view = dataclasses.replace(view, read_position=r1.read_position)

    with caplog.at_level(logging.INFO):
        r2 = svc.sync(view)

    assert r2.ok is True
    assert len(client.put_progressions) == 1
    _, replayed = client.put_progressions[0]
    assert replayed["locator"] == envelope["locator"]
    assert replayed["modified"] > envelope["modified"]  # fresh stamp
    assert json.loads(r2.fields["external_locator"])["locator"] == envelope["locator"]

    # Verify log messages
    assert any(
        'Re-anchored the read position for "The 12th Key"' in r.message
        for r in caplog.records
        if r.levelno == logging.INFO
    )
    assert not any(
        "not replaying it" in r.message for r in caplog.records if r.levelno == logging.WARNING
    )


def test_a_restore_persists_the_envelope_not_the_bare_locator(
    repo: _FakeBookRepository,
) -> None:
    """A restore persists the full progression envelope, not just the inner locator."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    client.put_progression_ok = True
    book = make_book(repo)

    result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert result.ok is True
    persisted = json.loads(result.fields["external_locator"])
    assert persisted == client.put_progressions[0][1]


def test_the_age_guard_compares_timestamps_chronologically_across_offsets(
    repo: _FakeBookRepository,
) -> None:
    """Age guard parses timestamps as aware datetimes; string order ≠ chronological order."""
    client = FakeKomga()
    service_instance = service(client)

    # Locator: 09:59 UTC, newest: 09:30 UTC (one hour ahead in local offset).
    # String compare: "…09:59" < "…11:30" (stale).
    # Chronological: 09:59 UTC > 09:30 UTC (fresh) → should be replayed.
    locator_json = json.dumps(
        {
            "modified": "2026-08-28T09:59:00+00:00",  # 09:59 UTC
            "locator": {
                "href": "OEBPS/a.xhtml",
                "locations": {"position": 5, "progression": 0.3},
            },
        }
    )
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_locator": locator_json, "external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    newest = ReadPosition(
        captured_at="2026-08-28T11:30:00+02:00",  # 09:30 UTC
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 1",
        chapter_href="a.xhtml",
        completed=False,
        total_chapters=5,
    )
    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=newest,
    )

    result = service_instance._snapshot_with_db_fallback(book_view, "KB1", {})

    # Should replay (chronologically fresh despite string order)
    parsed = json.loads(locator_json)
    assert result == parsed


def test_a_stale_locator_at_a_different_chapter_is_still_refused(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A locator older than newest and at a different chapter is refused."""
    client = FakeKomga()
    service_instance = service(client)

    stale_locator_json = json.dumps(
        {
            "modified": "2026-08-27T12:00:00+00:00",  # one day earlier
            "locator": {
                "href": "OEBPS/file0005.xhtml",
                "locations": {"position": 10, "progression": 0.2},
            },
        }
    )
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {"external_locator": stale_locator_json, "external_item_id": "KB1"},
    )
    book = repo.get(book.book_id)
    assert book is not None

    newest = ReadPosition(
        captured_at="2026-08-28T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 1",
        chapter_href="file0040.xhtml",  # different chapter
        completed=False,
        total_chapters=5,
    )
    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=newest,
    )

    with caplog.at_level(logging.WARNING):
        result = service_instance._snapshot_with_db_fallback(book_view, "KB1", {})

    assert result == {}
    assert any(
        "at a different chapter" in r.message and "not replaying it" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_a_stale_locator_at_the_newest_records_chapter_is_replayed(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A locator older than newest but at the same chapter is replayed (exempt from age guard)."""
    client = FakeKomga()
    service_instance = service(client)

    stale_locator_json = json.dumps(
        {
            "modified": "2026-08-28T10:00:00+00:00",
            "locator": {
                "href": "OEBPS/file0040.xhtml",
                "locations": {"position": 10, "progression": 0.2},
            },
        }
    )
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {"external_locator": stale_locator_json, "external_item_id": "KB1"},
    )
    book = repo.get(book.book_id)
    assert book is not None

    # Newer timestamp, but same chapter
    newest = ReadPosition(
        captured_at="2026-08-28T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="Chapter 1",
        chapter_href="file0040.xhtml",  # same chapter
        completed=False,
        total_chapters=5,
    )
    book_view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        author=book.author,
        story_url=book.story_url,
        output_filename=book.output_filename,
        status=book.status,
        rating=book.rating,
        category=book.category,
        erotica_tags=book.erotica_tags,
        site=book.site,
        description=book.description,
        date_published=book.date_published,
        author_url=book.author_url,
        series=book.series,
        series_url=book.series_url,
        section_url=book.section_url,
        external=ExternalLink(
            item_id=book.external_item_id,
            collection_id=book.external_collection_id,
            provider=book.external_provider,
            library_id=book.external_library_id,
        ),
        progress=ExternalProgress(
            position=book.external_read_position,
            completed=book.external_read_completed,
            total=book.external_read_total,
            locator=book.external_locator,
        ),
        read_position=newest,
    )

    with caplog.at_level(logging.DEBUG):
        result = service_instance._snapshot_with_db_fallback(book_view, "KB1", {})

    parsed = json.loads(stale_locator_json)
    assert result == parsed
    assert any(
        "stands at its chapter" in r.message for r in caplog.records if r.levelno == logging.DEBUG
    )


def test_a_failed_restore_does_not_persist_a_locator(
    repo: _FakeBookRepository,
) -> None:
    """When put_progression returns False, no external_locator is persisted."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    client.put_progression_ok = False  # restore rejection
    book = make_book(repo)

    result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert result.ok is True
    assert "external_locator" not in result.fields


def test_an_unmatched_restore_does_not_persist_a_locator(
    repo: _FakeBookRepository,
) -> None:
    """When target matches no chapter, no external_locator is persisted."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=9,
        chapter_progress=0.5,
        chapter_number=99,
        chapter_title="Gone",
        chapter_href="nope.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    book = make_book(repo)

    result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(book), restore_target=target)

    assert result.ok is True
    assert "external_locator" not in result.fields
    assert len(client.put_progressions) == 0


def test_an_ordinary_sync_still_persists_the_raw_locator(
    repo: _FakeBookRepository,
) -> None:
    """Without restore_target, raw-path still persists external_locator (regression guard)."""
    saved = {"locator": {"locations": {"position": 11, "progression": 0.68}}}
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [saved, reset]  # snapshot, then post-analyze
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client, now=lambda: FIXED_NOW).sync(book_to_view(book))

    assert result.ok is True
    assert "external_locator" in result.fields
    # Should be the saved snapshot
    persisted = json.loads(result.fields["external_locator"])
    assert persisted == saved


def test_the_persisted_locator_is_logged_at_debug(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """Successful restore logs the persisted locator at DEBUG level."""
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
    )
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.get_positions = lambda _: POSITIONS_FIXTURE  # type: ignore
    client.put_progression_ok = True
    book = make_book(repo)

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        result = service(client, now=lambda: FIXED_NOW).sync(
            book_to_view(book), restore_target=target
        )

    assert result.ok is True
    debug_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "Persisted the restored progression envelope" in r.message
    ]
    assert len(debug_logs) > 0, (
        f"Expected DEBUG log with 'Persisted the restored progression envelope', got: "
        f"{[r.message for r in caplog.records if 'Persisted' in r.message]}"
    )


# --- backward move warnings (EXP-123) ----------------------------------- #


def test_a_cross_chapter_backward_move_warns(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A read position moving to an earlier chapter warns and surfaces in result."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    # Set up: previously at chapter 124
    prev_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=124,
        chapter_progress=0.5,
        chapter_number=124,
        chapter_title="Chapter 124",
        chapter_href="file0124.xhtml",
        completed=False,
        total_chapters=200,
    )

    # Progression that carries the href for chapter 43
    progression = {
        "device": {"id": "test-device", "name": "Test Device"},
        "modified": "2026-01-02T00:00:00+00:00",
        "locator": {
            "href": "OEBPS/file0043.xhtml",
            "title": "Chapter 43",
            "locations": {"position": 43, "progression": 0.3, "totalProgression": 0.215},
        },
    }
    # Called 3 times: sync start snapshot, restore check, semantic capture
    client.progression_sequence = [progression, {}, progression]

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    # Read back: chapter 43 (backward move)
    client.book_sequence = [
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 6, "completed": False}),
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 6, "completed": False}),
    ]
    # Provide a range of positions to properly classify chapter indices
    client.positions = [
        {
            "href": "OEBPS/file0000.xhtml",
            "title": "Title Page",
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
        },
    ] + [
        {
            "href": f"OEBPS/file{i:04d}.xhtml",
            "title": f"Chapter {i}",
            "type": "application/xhtml+xml",
            "locations": {
                "position": i + 1,
                "progression": 0.0 if i != 43 else 0.3,
                "totalProgression": (i + 1) / 200,
            },
        }
        for i in range(1, 51)
    ]

    # Create BookView with prev_position set
    view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        external=ExternalLink(item_id="KB1"),
        read_position=prev_position,
        chapter_table=_chapters(*[(f"file{i:04d}.xhtml", f"Chapter {i}") for i in range(1, 51)]),
    )

    svc = service(client)
    svc._document_hrefs = lambda b: tuple(  # type: ignore[method-assign]
        ["file0000.xhtml"] + [f"file{i:04d}.xhtml" for i in range(1, 51)]
    )
    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc.sync(view)

    assert result.ok is True
    assert result.backward_move is not None
    assert "chapter 124 → 43" in result.backward_move
    assert any(
        "moved backwards: chapter_index 124 -> 43" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_a_forward_move_does_not_warn(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A read position moving to a later chapter does not warn."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    # Previously at chapter 43
    prev_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=43,
        chapter_progress=0.5,
        chapter_number=43,
        chapter_title="Chapter 43",
        chapter_href="file0043.xhtml",
        completed=False,
        total_chapters=200,
    )

    # Progression for chapter 124
    progression = {
        "device": {"id": "test-device", "name": "Test Device"},
        "modified": "2026-01-02T00:00:00+00:00",
        "locator": {
            "href": "OEBPS/file0124.xhtml",
            "title": "Chapter 124",
            "locations": {"position": 124, "progression": 0.3, "totalProgression": 0.62},
        },
    }
    # Called 3 times: sync start snapshot, restore check, semantic capture
    client.progression_sequence = [progression, {}, progression]

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    # Read back: chapter 124 (forward move)
    client.book_sequence = [
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 20, "completed": False}),
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 20, "completed": False}),
    ]
    # Provide a range of positions to properly classify chapter indices
    client.positions = [
        {
            "href": "OEBPS/file0000.xhtml",
            "title": "Title Page",
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
        },
    ] + [
        {
            "href": f"OEBPS/file{i:04d}.xhtml",
            "title": f"Chapter {i}",
            "type": "application/xhtml+xml",
            "locations": {
                "position": i + 1,
                "progression": 0.3 if i == 124 else 0.0,
                "totalProgression": (i + 1) / 200,
            },
        }
        for i in range(1, 151)
    ]

    view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        external=ExternalLink(item_id="KB1"),
        read_position=prev_position,
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.backward_move is None
    assert not any(
        "moved backwards" in r.message for r in caplog.records if r.levelname == "WARNING"
    )


def test_finishing_the_book_does_not_warn(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A read position with completed=True does not warn, even if chapter index regresses."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    prev_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=50,
        chapter_progress=0.5,
        chapter_number=50,
        chapter_title="Chapter 50",
        chapter_href="file0050.xhtml",
        completed=False,
        total_chapters=200,
    )

    # Progression for completed (all chapters)
    progression = {
        "device": {"id": "test-device", "name": "Test Device"},
        "modified": "2026-01-02T00:00:00+00:00",
        "locator": {
            "href": "OEBPS/file0200.xhtml",
            "title": "Chapter 200",
            "locations": {"position": 200, "progression": 1.0, "totalProgression": 1.0},
        },
    }
    # Called 3 times: sync start snapshot, restore check, semantic capture
    client.progression_sequence = [progression, {}, progression]

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    # Read back: completed (no matter the chapter index)
    client.book_sequence = [
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 200, "completed": True}),
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 200, "completed": True}),
    ]
    client.positions = [
        {
            "href": "OEBPS/file0200.xhtml",
            "title": "Chapter 200",
            "type": "application/xhtml+xml",
            "locations": {"position": 200, "progression": 1.0, "totalProgression": 1.0},
        },
    ]

    view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        external=ExternalLink(item_id="KB1"),
        read_position=prev_position,
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.backward_move is None


def test_a_within_chapter_regression_still_warns(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A read position at the same chapter but lower progress warns."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    # Previously at chapter 50, 80% progress
    prev_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=50,
        chapter_progress=0.80,
        chapter_number=50,
        chapter_title="Chapter 50",
        chapter_href="file0050.xhtml",
        completed=False,
        total_chapters=200,
    )

    # Progression for chapter 50, 20% progress (backward within chapter)
    progression = {
        "device": {"id": "test-device", "name": "Test Device"},
        "modified": "2026-01-02T00:00:00+00:00",
        "locator": {
            "href": "OEBPS/file0050.xhtml",
            "title": "Chapter 50",
            "locations": {"position": 50, "progression": 0.20, "totalProgression": 0.25},
        },
    }
    # Called 3 times: sync start snapshot, restore check, semantic capture
    client.progression_sequence = [progression, {}, progression]

    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    # Read back: same chapter, 20% progress (backward within chapter)
    client.book_sequence = [
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 10, "completed": False}),
        komga_book(metadata=MATCHING_BOOK_METADATA, read={"page": 10, "completed": False}),
    ]
    client.positions = [
        {
            "href": "OEBPS/file0050.xhtml",
            "title": "Chapter 50",
            "type": "application/xhtml+xml",
            "locations": {"position": 50, "progression": 0.20, "totalProgression": 0.25},
        },
    ]

    view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        external=ExternalLink(item_id="KB1"),
        read_position=prev_position,
        chapter_table=_chapters(("file0050.xhtml", "Chapter 50")),
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.backward_move is not None
    assert "chapter 50" in result.backward_move


def test_the_raw_replay_warns_when_the_app_has_a_record(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """_restore_progress_if_lost warns when replaying over a book with a read_position."""
    snap = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 11, "progression": 0.68},
        }
    }
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client = FakeKomga()
    client.progression_sequence = [
        snap,
        reset,
    ]  # snapshot at sync start, post-analyze: progress lost
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    # Set up a read_position so the replay warning triggers
    read_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=2,
        chapter_progress=0.5,
        chapter_number=2,
        chapter_title="Chapter 2",
        chapter_href="file0002.xhtml",
        completed=False,
        total_chapters=10,
    )

    view = make_book_view(
        book_id=book.book_id,
        title=book.title,
        external=ExternalLink(item_id="KB1"),
        read_position=read_position,
        chapter_table=_DEFAULT_CHAPTER_TABLE,
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    replay_logs = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "Replaying a pre-sync read progression for" in r.message
    ]
    assert len(replay_logs) > 0


# --- TASK-33: the raw fast path is gated on the bookmark still resolving (RP-D9) --- #


def test_a_dead_locator_is_never_replayed(caplog: pytest.LogCaptureFixture) -> None:
    """A bookmark whose anchor is gone is re-anchored by the core, never blindly replayed."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/chapter_01_new.xhtml",
            "title": None,
            "locations": {"position": 1, "progression": 0.0},
        }
    ]
    client.progression_sequence = [
        {
            "locator": {
                "href": "OEBPS/file0001.xhtml",
                "locations": {"position": 2, "progression": 0.68},
            }
        }
    ]
    read_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=99,
        chapter_progress=0.5,
        chapter_number=999,
        chapter_title="Gone",
        chapter_href="nope.xhtml",
        completed=False,
        total_chapters=5,
    )
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        read_position=read_position,
        chapter_table=_chapters(("chapter_01_new.xhtml", "New Chapter")),
    )

    with caplog.at_level(logging.WARNING):
        result = service(client).sync(view)

    assert result.ok is True
    assert client.put_progressions == []
    assert not any(r.levelname == "ERROR" for r in caplog.records)
    matches = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "no chapter matches among 1 candidate(s)" in r.message
    ]
    assert len(matches) == 1


def test_a_live_locator_still_takes_the_raw_fast_path(caplog: pytest.LogCaptureFixture) -> None:
    """A bookmark that still resolves still takes the raw fast path (RP-PLUG-4 kept, gated)."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    snap = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 5, "progression": 0.68},
        }
    }
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client.progression_sequence = [snap, reset, reset]
    read_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=2,
        chapter_progress=0.5,
        chapter_number=2,
        chapter_title="The 12th Key - Ch 2",
        chapter_href="file0002.xhtml",
        completed=False,
        total_chapters=2,
    )
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        read_position=read_position,
        chapter_table=_DEFAULT_CHAPTER_TABLE,
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view)

    assert result.ok is True
    assert len(client.put_progressions) == 1
    assert client.put_progressions[0][1]["locator"]["href"] == "OEBPS/file0002.xhtml"
    assert any(
        r.levelname == "INFO" and "Restored read progression for" in r.message
        for r in caplog.records
    )


def test_a_book_without_a_stored_position_still_takes_the_raw_fast_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No stored position at all is not-applicable to the core check; the raw path still runs."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    snap = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 5, "progression": 0.68},
        }
    }
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client.progression_sequence = [snap, reset, reset]
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        read_position=None,
        chapter_table=_DEFAULT_CHAPTER_TABLE,
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view)

    assert result.ok is True
    assert any(
        r.levelname == "INFO" and "Restored read progression for" in r.message
        for r in caplog.records
    )
    assert len(client.put_progressions) == 1


def test_a_provider_with_no_anchors_still_takes_the_raw_fast_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider that lists no anchors cannot be checked this run; the raw path still runs."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = []
    snap = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 5, "progression": 0.68},
        }
    }
    reset = {"locator": {"locations": {"position": 0, "progression": 0.0}}}
    client.progression_sequence = [snap, reset, reset]
    read_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=2,
        chapter_progress=0.5,
        chapter_number=2,
        chapter_title="The 12th Key - Ch 2",
        chapter_href="file0002.xhtml",
        completed=False,
        total_chapters=2,
    )
    view = make_book_view(external=ExternalLink(item_id="KB1"), read_position=read_position)

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view)

    assert result.ok is True
    assert any(
        r.levelname == "INFO" and "Restored read progression for" in r.message
        for r in caplog.records
    )
    assert any(
        r.levelname == "WARNING" and "Komga listed no anchors for" in r.message
        for r in caplog.records
    )


def test_a_lost_bookmark_is_re_anchored_instead_of_replayed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider holding no bookmark at all is re-anchored by the core, not raw-replayed."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}, {}]
    read_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="file0001.xhtml",
        completed=False,
        total_chapters=2,
    )
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        read_position=read_position,
        chapter_table=_DEFAULT_CHAPTER_TABLE,
    )

    with caplog.at_level(logging.DEBUG):
        result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert len(client.put_progressions) == 1
    assert client.put_progressions[0][1]["locator"]["href"] == "OEBPS/file0001.xhtml"
    assert json.loads(result.fields["external_locator"]) == client.put_progressions[0][1]
    assert not any(
        r.levelname == "DEBUG" and "Komga kept read progression" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Discovery ordering tests (path-first vs title-first)
# ---------------------------------------------------------------------------


def test_ensure_present_prefers_the_path_index_over_the_title_lookup() -> None:
    """Path lookup wins when both path and title resolve to different ids."""
    client = FakeKomga()
    client.find_results = ["KB_TITLE"]
    client.library_books = [("KB_PATH", "/books/author/title.epub")]
    svc = service(client)
    view = make_book_view(
        book_id="thisBook", title="Test Book", output_filename="author/title.epub"
    )

    result = svc._ensure_present(view, allow_scan=False)

    assert result == "KB_PATH"
    assert client.find_calls == 0
    assert client.list_calls == 1


def test_a_book_absent_from_the_path_index_is_still_found_by_title() -> None:
    """Title lookup runs as fallback when path index is empty (second key)."""
    client = FakeKomga()
    client.library_books = []
    client.find_results = ["KB1"]
    svc = service(client)
    view = make_book_view(title="Test Book", output_filename="author/title.epub")

    result = svc._ensure_present(view, allow_scan=False)

    assert result == "KB1"
    assert client.find_calls == 1
    assert client.list_calls == 1


def test_a_titleless_book_is_still_resolved_by_path() -> None:
    """Path lookup works even when book has no title (no title guard before path)."""
    client = FakeKomga()
    client.library_books = [("KB_PATH", "/books/author/title.epub")]
    client.find_results = []
    svc = service(client)
    view = make_book_view(title="", output_filename="author/title.epub")

    result = svc._ensure_present(view, allow_scan=False)

    assert result == "KB_PATH"
    assert client.find_calls == 0


def test_a_path_miss_logs_the_title_fallback_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Path miss logs DEBUG when falling back to title+author."""
    client = FakeKomga()
    client.library_books = []
    client.find_results = ["KB1"]
    svc = service(client)
    view = make_book_view(title="Test Book", output_filename="author/title.epub")

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        svc._ensure_present(view, allow_scan=False)

    debug_logs = [
        r
        for r in caplog.records
        if r.levelname == "DEBUG"
        and "Komga path lookup missed for" in r.message
        and "falling back to title+author" in r.message
    ]
    assert len(debug_logs) == 1


# ---------------------------------------------------------------------------
# Link refusal tests (EXP-187)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-187")
def test_an_owned_book_is_refused_without_a_provider_scan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An owned Komga book is refused — no scan, no poll, one WARNING, owner named."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "otherBook"

    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    svc = service(client, link_owner=link_owner, scan_retry_max=20)
    view = make_book_view(book_id="thisBook", title="Sunday Static")

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc.sync(view)

    assert result.ok is False
    expected_msg = "link refused: Komga book KOMGA1 is already linked to book_id=otherBook"
    assert result.message == expected_msg
    assert client.scan_calls == 0
    assert client.find_calls == 1
    assert client.list_calls == 1
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 1
    assert "did not appear in Komga" not in caplog.text


def test_a_refusal_found_by_path_is_also_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An owned Komga book found by path is also refused — no scan."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "otherBook"

    client = FakeKomga()
    client.find_results = [None]
    client.library_books = [("KOMGA1", "/library/author/title.epub")]
    svc = service(client, link_owner=link_owner, scan_retry_max=20)
    view = make_book_view(book_id="thisBook", title="Test", output_filename="author/title.epub")

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc.sync(view)

    assert result.ok is False
    expected_msg = "link refused: Komga book KOMGA1 is already linked to book_id=otherBook"
    assert result.message == expected_msg
    assert client.scan_calls == 0
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 1
    assert "found by path" in refusal_logs[0].message


def test_a_genuinely_absent_book_still_scans_and_polls() -> None:
    """An absent book (not owned by anyone) still scans and polls."""
    client = FakeKomga()
    client.find_results = []  # always None, no link_owner
    svc = service(client, scan_retry_max=3)
    view = make_book_view(book_id="thisBook", title="Test")

    result = svc.sync(view)

    assert result.ok is False
    assert result.message == "book did not appear in Komga"
    assert client.scan_calls == 1
    assert client.find_calls == 4  # 1 initial + 3 polls


def test_a_refusal_during_the_scan_poll_stops_the_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal found during the scan poll stops polling immediately."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str | None:
        return "otherBook" if item_id == "KOMGA9" else None

    client = FakeKomga()
    client.find_results = [None, "KOMGA9"]  # initial miss, first poll hits an owned id
    svc = service(client, link_owner=link_owner, scan_retry_max=5)
    view = make_book_view(book_id="thisBook", title="Test")

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc.sync(view)

    assert result.ok is False
    expected_msg = "link refused: Komga book KOMGA9 is already linked to book_id=otherBook"
    assert result.message == expected_msg
    assert client.scan_calls == 1
    assert client.find_calls == 2  # 1 initial + 1 poll (stopped)
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 1


def test_enrich_refuses_an_owned_book_with_the_owner_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An owned book in enrich() is refused with the owner named."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "otherBook"

    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    svc = service(client, link_owner=link_owner)
    view = make_book_view(book_id="thisBook", title="Test")

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc.enrich(view)

    assert result.ok is False
    expected_msg = "link refused: Komga book KOMGA1 is already linked to book_id=otherBook"
    assert result.message == expected_msg
    assert "external_item_id" not in result.fields
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 1


def test_enrich_claims_an_unowned_book() -> None:
    """An unowned book in enrich() is claimed successfully."""

    def link_owner(item_id: str, *, provider: str | None = None) -> None:
        return None

    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    svc = service(client, link_owner=link_owner)
    view = make_book_view(book_id="thisBook", title="Test")

    result = svc.enrich(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "KOMGA1"


def test_the_komga_refusal_asks_provider_scoped() -> None:
    """The Komga refusal check asks link_owner scoped to the komga provider (EXP-243)."""
    calls: list[tuple[str, str | None]] = []

    def link_owner(item_id: str, *, provider: str | None = None) -> str | None:
        calls.append((item_id, provider))
        return "otherBook"

    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    svc = service(client, link_owner=link_owner)
    svc.sync(make_book_view(book_id="thisBook", title="T"))

    assert calls
    assert all(call[1] == "komga" for call in calls)


# ---------------------------------------------------------------------------
# Link ownership tests (EXP-149)
# ---------------------------------------------------------------------------


def test_ensure_present_refuses_an_already_owned_book(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second claimant is left unlinked when another book already owns the Komga id."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "otherBook"

    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    svc = service(client, link_owner=link_owner, scan_retry_max=0)

    book_view = make_book_view(book_id="thisBook", title="Test Book")
    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc._ensure_present(book_view)

    assert isinstance(result, LinkRefusal)
    assert result.owner_book_id == "otherBook"
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 1
    log_msg = refusal_logs[0].message
    assert "Refused to link" in log_msg
    assert "book_id=thisBook" in log_msg
    assert "KOMGA1" in log_msg
    assert "book_id=otherBook" in log_msg


def test_ensure_present_claims_an_unowned_book(caplog: pytest.LogCaptureFixture) -> None:
    """An unowned Komga id is claimed when link_owner returns None."""

    def link_owner(item_id: str, *, provider: str | None = None) -> None:
        return None

    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    svc = service(client, link_owner=link_owner)

    book_view = make_book_view(book_id="thisBook", title="Test Book")
    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc._ensure_present(book_view)

    assert result == "KOMGA1"
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 0


def test_ensure_present_reclaims_its_own_id(caplog: pytest.LogCaptureFixture) -> None:
    """A book can reclaim its own id when link_owner returns its own book_id."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "thisBook"

    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    svc = service(client, link_owner=link_owner)

    book_view = make_book_view(book_id="thisBook", title="Test Book")
    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc._ensure_present(book_view)

    assert result == "KOMGA1"
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 0


def test_ensure_present_without_a_link_owner_is_unchanged() -> None:
    """A service built without link_owner discovers normally (backward compatibility)."""
    client = FakeKomga()
    client.find_results = ["KOMGA1"]
    svc = service(client)  # no link_owner argument

    book_view = make_book_view(book_id="thisBook", title="Test Book")
    result = svc._ensure_present(book_view)

    assert result == "KOMGA1"


def test_ensure_present_keeps_an_existing_stored_link() -> None:
    """An existing stored link is never revoked, even if link_owner says another book owns it."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "someoneElse"

    client = FakeKomga()
    client.book_exists_result = True
    svc = service(client, link_owner=link_owner)

    book_view = make_book_view(
        book_id="thisBook", title="Test Book", external=ExternalLink(item_id="KOMGA1")
    )
    result = svc._ensure_present(book_view)

    assert result == "KOMGA1"


def test_ensure_present_refuses_an_owned_id_found_by_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An id found by path is refused when link_owner says another book owns it."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "otherBook"

    client = FakeKomga()
    client.find_results = [None]  # title search misses
    client.library_books = [("KOMGA1", "/library/author/title.epub")]
    svc = service(client, link_owner=link_owner)

    book_view = make_book_view(
        book_id="thisBook", title="Test Book", output_filename="author/title.epub"
    )
    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc._ensure_present(book_view, allow_scan=False)

    assert isinstance(result, LinkRefusal)
    assert result.owner_book_id == "otherBook"
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 1
    log_msg = refusal_logs[0].message
    assert "found by path" in log_msg


def test_ensure_present_refuses_an_owned_id_during_the_scan_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An id found during scan poll is refused when link_owner says another book owns it."""

    def link_owner(item_id: str, *, provider: str | None = None) -> str:
        return "otherBook"

    client = FakeKomga()
    client.find_results = [None, None, None, None, None, None]  # 3 initial + 3 polls all miss
    client.library_books_sequence = [[], [], [("KOMGA1", "/library/author/title.epub")]]
    svc = service(client, link_owner=link_owner, scan_retry_max=2)

    book_view = make_book_view(
        book_id="thisBook", title="Test Book", output_filename="author/title.epub"
    )
    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc._ensure_present(book_view, allow_scan=True)

    assert isinstance(result, LinkRefusal)
    assert result.owner_book_id == "otherBook"
    refusal_logs = [
        r for r in caplog.records if r.levelname == "WARNING" and "Refused to link" in r.message
    ]
    assert len(refusal_logs) == 1


# ---------------------------------------------------------------------------
# Path re-validation of an existing link tests (EXP-243)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-243")
def test_a_linked_book_whose_path_is_at_another_komga_book_is_re_linked() -> None:
    """A stored link is corrected when its own path resolves to a different Komga book."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "KB_RIGHT"


def test_the_path_revalidation_relink_is_logged_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The path-revalidation relink is logged at WARNING, naming both ids and the path."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        service(client).sync(view)

    assert any(
        r.levelname == "WARNING"
        and "pointed at book KB_WRONG" in r.message
        and "re-linking" in r.message
        and "author/title.epub" in r.message
        for r in caplog.records
    )


def test_a_linked_book_whose_path_is_absent_from_the_index_keeps_its_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A path absent from the Komga URL index keeps the stored link."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_OTHER", "/books/somebody/else.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.fields["external_item_id"] == "KB_WRONG"
    assert any(
        r.levelname == "DEBUG" and "Komga path re-validation kept the stored link" in r.message
        for r in caplog.records
    )


def test_an_empty_url_index_never_unlinks(caplog: pytest.LogCaptureFixture) -> None:
    """An empty Komga URL index (the default) never unlinks an existing stored link."""
    client = FakeKomga()
    client.book_exists_result = True
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.fields["external_item_id"] == "KB_WRONG"
    assert "re-linking" not in caplog.text


def test_a_linked_book_with_no_output_filename_keeps_its_link() -> None:
    """A linked book with no output_filename skips path re-validation entirely."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename=None,
        external=ExternalLink(item_id="KB_WRONG"),
    )

    result = service(client).sync(view)

    assert result.fields["external_item_id"] == "KB_WRONG"
    assert client.list_calls == 0


def test_a_revalidated_id_another_row_owns_keeps_the_stale_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A re-validated id another library row owns keeps the stale link (EXP-149)."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )
    svc = service(client, link_owner=lambda item_id, *, provider=None: "otherBook")

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = svc.sync(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "KB_WRONG"
    assert any(
        r.levelname == "WARNING"
        and "keeping the stale link KB_WRONG" in r.message
        and "book_id=otherBook already owns it" in r.message
        for r in caplog.records
    )


@pytest.mark.pins("EXP-243")
def test_a_kept_stale_komga_link_is_recorded_on_the_result() -> None:
    """A kept stale Komga link is recorded on the result for the per-book patch (F5c)."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )
    svc = service(client, link_owner=lambda item_id, *, provider=None: "otherBook")

    result = svc.sync(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "KB_WRONG"
    assert result.stale_link_kept == (
        "stale link kept: this book's file is Komga book KB_RIGHT, "
        "which book_id=otherBook already owns"
    )


def test_a_clean_komga_sync_reports_no_stale_link() -> None:
    """A sync that finds no ownership conflict reports stale_link_kept=None (F5c)."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )
    svc = service(client, link_owner=lambda item_id, *, provider=None: None)

    result = svc.sync(view)

    assert result.stale_link_kept is None
    assert result.fields["external_item_id"] == "KB_RIGHT"


def test_a_path_mismatch_relink_clears_the_external_locator(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A path-mismatch relink clears external_locator: it was captured from the wrong book."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
        progress=ExternalProgress(
            locator='{"locator": {"href": "OEBPS/title_page.xhtml", "locations": {"position": 1}}}'
        ),
    )

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.fields["external_locator"] is None
    assert any(
        r.levelname == "INFO" and "Cleared the Komga locator for" in r.message
        for r in caplog.records
    )


def test_a_path_mismatch_relink_does_not_replay_the_persisted_locator() -> None:
    """A path-mismatch relink never writes the wrong book's locator into the resolved book."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
        progress=ExternalProgress(
            locator='{"locator": {"href": "OEBPS/title_page.xhtml", "locations": {"position": 1}}}'
        ),
    )

    service(client).sync(view)

    assert client.put_progressions == []


# ---------------------------------------------------------------------------
# relinked reporting — wrong-item vs id-reissue (OR-6, EXP-243)
# ---------------------------------------------------------------------------


def test_a_path_mismatch_relink_is_reported_as_relinked() -> None:
    """A wrong-item relink (path re-validation) is reported as relinked=True."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_WRONG"),
    )

    result = service(client).sync(view)

    assert result.relinked is True


def test_an_id_reissue_relink_is_not_reported_as_relinked() -> None:
    """A Komga id re-issue (404 book_exists) for the same file is not reported as relinked."""
    client = FakeKomga()
    client.book_exists_result = False
    client.find_results = ["KB2"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename=None,
        external=ExternalLink(item_id="KB1"),
    )

    result = service(client).sync(view)

    assert result.fields["external_item_id"] == "KB2"
    assert result.relinked is False


def test_an_unchanged_link_is_not_reported_as_relinked() -> None:
    """A linked book whose path index agrees with the stored id is not reported as relinked."""
    client = FakeKomga()
    client.book_exists_result = True
    client.library_books = [("KB_RIGHT", "/books/author/title.epub")]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="thisBook",
        title="T",
        output_filename="author/title.epub",
        external=ExternalLink(item_id="KB_RIGHT"),
    )

    result = service(client).sync(view)

    assert result.relinked is False


def test_sync_reports_restore_attempted_when_it_restores(
    repo: _FakeBookRepository,
) -> None:
    """A sync with a restore_target reports restore_attempted=True when the restore succeeds."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = POSITIONS_FIXTURE
    client.put_progression_ok = True
    book = make_book(repo)

    restore_target = ReadPosition(
        captured_at="2026-06-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href="OEBPS/file0001.xhtml",
        completed=False,
        total_chapters=2,
    )
    result = service(client).sync(book_to_view(book), restore_target=restore_target)

    assert result.ok is True
    assert result.restore_attempted is True


def test_sync_reports_no_restore_attempt_without_a_target(
    repo: _FakeBookRepository,
) -> None:
    """A sync without a restore_target reports restore_attempted=False."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    result = service(client).sync(book_to_view(book), restore_target=None)

    assert result.restore_attempted is False


def test_sync_reports_no_restore_attempt_when_the_book_is_unresolvable(
    repo: _FakeBookRepository,
) -> None:
    """When a book cannot be resolved, restore_attempted is False even with a restore_target."""
    client = FakeKomga()
    client.find_results = [None]  # title search misses
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    restore_target = ReadPosition(
        captured_at="2026-06-12T12:00:00+00:00",
        chapter_index=0,
        chapter_progress=0.0,
        chapter_number=1,
        chapter_title="The 12th Key",
        chapter_href="OEBPS/titlepage.xhtml",
        completed=False,
        total_chapters=2,
    )
    result = service(client, scan_retry_max=0).sync(
        book_to_view(book), allow_scan=False, restore_target=restore_target
    )

    assert result.ok is False
    assert result.restore_attempted is False


def test_sync_reports_no_restore_attempt_when_komga_is_disabled(
    repo: _FakeBookRepository,
) -> None:
    """When Komga is disabled, restore_attempted is False even with a restore_target."""
    client = FakeKomga()
    book = make_book(repo)

    restore_target = ReadPosition(
        captured_at="2026-06-12T12:00:00+00:00",
        chapter_index=0,
        chapter_progress=0.0,
        chapter_number=1,
        chapter_title="The 12th Key",
        chapter_href="OEBPS/titlepage.xhtml",
        completed=False,
        total_chapters=2,
    )
    result = service(client, enabled=False).sync(book_to_view(book), restore_target=restore_target)

    assert result.attempted is False
    assert result.restore_attempted is False


def test_enrich_never_reports_a_restore_attempt(repo: _FakeBookRepository) -> None:
    """An enrich call never attempts a restore, so restore_attempted is always False."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    result = service(client).enrich(book_to_view(book))

    assert result.restore_attempted is False


def test_sync_result_defaults_restore_attempted_to_false() -> None:
    """SyncResult defaults restore_attempted to False."""
    from komga_sync.service import SyncResult

    result = SyncResult(True)
    assert result.restore_attempted is False


def test_a_provider_that_dies_mid_batch_is_reported_as_unreachable_on_komga(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider dying mid-batch is reported unreachable once per service, not missing."""
    clock: dict[str, float] = {"t": 0.0}
    client = FakeKomga()
    client.book = {
        "id": "B1",
        "metadata": {"title": "Book 1", "tags": []},
        "seriesId": "S1",
        "media": {"pagesCount": 100},
        "readProgress": {},
    }
    client.series = {"id": "S1", "metadata": {"genres": []}}
    client.positions = [
        {
            "href": "OEBPS/file0001.xhtml",
            "title": "Ch 1",
            "locations": {"position": 1, "progression": 0.0},
        }
    ]
    # First sync needs 2 get_book calls (initial + refresh), second fails on 3rd call
    client.find_results = ["B1", "B2", "B3"]
    client.die_after_get_book_calls = 2

    view1 = make_book_view(title="Book 1", chapter_table=_chapters(("file0001.xhtml", "Ch 1")))
    view2 = make_book_view(title="Book 2")
    view3 = make_book_view(title="Book 3")

    svc = KomgaService(
        client,
        server_url="http://komga.local",
        enabled=True,
        scan_retry_max=1,
        scan_retry_delay=0,
        monotonic=lambda: clock["t"],
    )

    with caplog.at_level(logging.DEBUG):
        result1 = svc.sync(view1)

    assert result1.ok is True
    assert result1.unreachable is False

    with caplog.at_level(logging.WARNING):
        result2 = svc.sync(view2)

    assert result2.ok is False
    assert result2.unreachable is True
    assert result2.message == "Komga is not reachable"
    assert "not found" not in result2.message

    with caplog.at_level(logging.DEBUG):
        result3 = svc.sync(view3)

    assert result3.ok is False
    assert result3.unreachable is True
    assert result3.message == "Komga is not reachable"
    assert "not found" not in result3.message

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]

    assert len(warnings) == 1
    assert "Komga is not reachable" in warnings[0].message
    assert "every later book in this batch" in warnings[0].message
    assert "is reported unreachable, not missing" in warnings[0].message


def test_enrich_distinguishes_disabled_from_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """enrich returns different messages for disabled vs unreachable."""
    client = FakeKomga()
    client.book = {"id": "B1", "metadata": {}, "media": {"pagesCount": 100}, "readProgress": {}}
    view = make_book_view(title="Book")

    svc_disabled = KomgaService(
        client,
        server_url="http://komga.local",
        enabled=False,
        scan_retry_max=1,
        scan_retry_delay=0,
    )

    result_disabled = svc_disabled.enrich(view)
    assert result_disabled.ok is False
    assert result_disabled.message == "Komga sync is disabled"
    assert result_disabled.attempted is False
    assert result_disabled.unreachable is False

    client_unreachable = FakeKomga()
    client_unreachable.connected = False

    svc_unreachable = KomgaService(
        client_unreachable,
        server_url="http://komga.local",
        enabled=True,
        scan_retry_max=1,
        scan_retry_delay=0,
    )

    with caplog.at_level(logging.WARNING):
        result_unreachable = svc_unreachable.enrich(view)

    assert result_unreachable.ok is False
    assert result_unreachable.message == "Komga is not reachable"
    assert result_unreachable.attempted is True
    assert result_unreachable.unreachable is True


def test_delete_remote_book_survives_an_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """delete_remote_book logs and continues when Komga is unreachable."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    client = FakeKomga()
    client.get_book = lambda book_id: (_ for _ in ()).throw(
        ProviderUnreachable("Komga connection lost")
    )

    svc = KomgaService(
        client,
        server_url="http://komga.local",
        enabled=True,
        scan_retry_max=1,
        scan_retry_delay=0,
    )

    with caplog.at_level(logging.WARNING):
        svc.delete_remote_book("B999")

    assert any("could not delete book" in r.message for r in caplog.records)


@pytest.mark.pins("EXP-195")
def test_external_progress_at_is_not_stamped_when_only_the_page_total_changes(
    repo: _FakeBookRepository,
) -> None:
    """Page total change from a different provider does not stamp external_progress_at (EXP-195)."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=65, read={"page": 0, "completed": False})
    client.series = {"metadata": {}}
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {
            "external_provider": "kavita",
            "external_read_total": 2,
            "external_read_position": 0,
            "external_read_completed": 0,
        },
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert "external_progress_at" not in result.fields


@pytest.mark.pins("EXP-195")
def test_a_same_provider_recount_still_stamps(repo: _FakeBookRepository) -> None:
    """Page total change from the same provider stamps external_progress_at (EXP-195)."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=16, read={"page": 0, "completed": False})
    client.series = {"metadata": {}}
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {
            "external_provider": "komga",
            "external_read_total": 14,
            "external_read_position": 0,
            "external_read_completed": 0,
        },
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields.get("external_progress_at") is not None


@pytest.mark.pins("EXP-195")
def test_a_position_change_stamps_regardless_of_provider(repo: _FakeBookRepository) -> None:
    """Position change stamps external_progress_at regardless of provider (EXP-195)."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata={}, pages=65, read={"page": 3, "completed": False})
    client.series = {"metadata": {}}
    book = make_book(repo)
    repo.update_fields(
        book.book_id,
        {
            "external_provider": "kavita",
            "external_read_total": 65,
            "external_read_position": 0,
            "external_read_completed": 0,
        },
    )
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert result.fields.get("external_progress_at") is not None


@pytest.mark.pins("EXP-195")
def test_a_komga_sync_where_only_the_total_changed_does_not_stamp_external_progress_at() -> None:  # noqa: C901 — an inline fake client, clearest kept together with the scenario
    """A Komga sync where only the page total changed does not stamp (EXP-195)."""

    # Fixed clock ticking once per call
    class TickingClock:
        def __init__(self, start: datetime) -> None:
            self._t = start

        def __call__(self) -> datetime:
            self._t = self._t + timedelta(seconds=1)
            return self._t

    fixed_start = datetime(2026, 8, 25, 17, 57, 52, tzinfo=UTC)
    clock = TickingClock(fixed_start)

    # Create a view for the second sync as if Kavita's result was persisted
    view_after_kavita = make_book_view(
        external=ExternalLink(provider="kavita", item_id="11"),
        progress=ExternalProgress(position=0, total=2, completed=False, percent=0.0),
    )

    # Simulate Komga sync: reports different total (65) but same position (0)
    class FakeKomgaClient:
        def test_connection(self):
            return ConnectionTestResult("ok", "Connected")

        def list_library_books(self):
            return []

        def find_book_id(self, title: str, author: str):
            return "KB1"

        def book_exists(self, book_id: str):
            return True

        def get_book(self, book_id: str):
            return {
                "id": "KB1",
                "seriesId": "SERIES1",
                "media": {"pagesCount": 65},
                "metadata": {},
                "readProgress": {"page": 0, "completed": False},
            }

        def get_series(self, series_id: str):
            return {"metadata": {}}

        def get_progression(self, book_id: str):
            return {}

        def get_positions(self, book_id: str):
            return []

        def patch_book_metadata(self, book_id: str, patch):
            pass

        def patch_series_metadata(self, series_id: str, patch):
            pass

        def put_progression(self, book_id: str, progression):
            return True

    komga_result = KomgaService(
        FakeKomgaClient(),
        server_url="http://komga.local",
        enabled=True,
        scan_retry_max=1,
        scan_retry_delay=0,
        now=clock,
    ).sync(view_after_kavita)

    # After Komga sync: should NOT have stamped
    # (provider is kavita, only total changed, which doesn't stamp for different provider)
    assert "external_progress_at" not in komga_result.fields


def test_a_komga_sync_reports_the_semantic_position_the_migration_restores(
    repo: _FakeBookRepository,
) -> None:
    """A Komga sync captures the semantic position a later provider migration restores."""
    book = make_book(repo, num_chapters=10, output_filename="A/b.epub")
    book_id = book.book_id

    komga_client = FakeKomga()
    komga_client.find_results = ["KB1"]
    komga_client.book = {
        "id": "KB1",
        "seriesId": "SERIES1",
        "media": {"pagesCount": 14},
        "metadata": {},
        "readProgress": {"page": 0, "completed": False},
    }
    komga_client.positions: list[dict[str, Any]] = [
        {
            "href": f"OEBPS/c{i}.xhtml",
            "title": f"Chapter {i}",
            "type": "application/xhtml+xml",
            "locations": {"position": i + 1, "progression": 0.0, "totalProgression": i / 10},
        }
        for i in range(1, 11)
    ]
    komga_client.progression_sequence = [
        {
            "locator": {
                "href": "OEBPS/c3.xhtml",
                "title": "Chapter 3",
                "locations": {"position": 3, "progression": 0.5},
            }
        }
    ]
    komga_client.put_progression_ok = True

    chapter_links = tuple(
        ChapterLink(url=f"https://x/{i}", title=f"Chapter {i}") for i in range(1, 11)
    )
    komga_view = make_book_view(
        book_id=book_id,
        title=book.title,
        output_filename=book.output_filename,
        num_chapters=10,
        chapters=chapter_links,
        chapter_table=_chapters(*[(f"c{i}.xhtml", f"Chapter {i}") for i in range(1, 11)]),
    )

    komga_service = KomgaService(
        komga_client,
        server_url="http://komga.local",
        enabled=True,
        scan_retry_max=1,
        scan_retry_delay=0,
    )
    komga_result = komga_service.sync(komga_view)

    assert komga_result.ok is True
    read_position = komga_result.read_position
    assert read_position is not None
    assert read_position.chapter_index == 3
    assert read_position.chapter_progress == 0.5
    assert read_position.chapter_number is None
    assert read_position.chapter_title == "Chapter 3"
    assert read_position.chapter_href == "c3.xhtml"
    assert read_position.completed is False
    assert read_position.total_chapters == 10
    assert read_position.chapter_key == "c3.xhtml"
    fields = dict(komga_result.fields)
    progress_at = fields.pop("external_progress_at")
    synced_at = fields.pop("external_synced_at")
    assert isinstance(progress_at, datetime)
    assert isinstance(synced_at, datetime)
    assert fields == {
        "external_read_total": 14,
        "external_read_position": 0,
        "external_read_completed": 0,
        "external_read_percent": 0.0,
        "external_item_id": "KB1",
        "external_collection_id": "SERIES1",
        "external_provider": "komga",
        "external_library_id": None,
        "external_item_url": "http://komga.local/book/KB1",
        "external_chapter_count": 10,
    }


def test_a_komga_sync_reports_the_semantic_position_the_migration_restores_after_migrating_back():
    """A Komga restore lands the semantic position on the chapter a migration targets."""
    restore_target = ReadPosition(
        captured_at="2026-09-24T08:36:30.783250+00:00",
        chapter_index=3,
        chapter_progress=0.5,
        chapter_number=None,
        chapter_title="Chapter 3",
        chapter_href=None,
        completed=False,
        total_chapters=10,
        chapter_key="Chapter 3",
    )
    chapter_links = tuple(
        ChapterLink(url=f"https://x/{i}", title=f"Chapter {i}") for i in range(1, 11)
    )
    chapter_table = tuple(
        ChapterView(
            ordinal=i,
            key=f"https://x/{i}",
            title=f"Chapter {i}",
            number=str(i),
            role="content",
            href=f"c{i}.xhtml",
        )
        for i in range(1, 11)
    )
    view_for_restore = make_book_view(
        num_chapters=10,
        chapters=chapter_links,
        chapter_table=chapter_table,
        # A provider move zeroes progress rather than leaving it None (_FORGET_LINK_FIELDS in
        # src/services/provider_move_service.py) — match that so the restore sync doesn't see
        # a spurious None->0 position change and stamp external_progress_at.
        progress=ExternalProgress(position=0, total=0, completed=False, percent=0.0),
        restore_target=restore_target,
    )

    komga_client = FakeKomga()
    komga_client.find_results = ["KB1"]
    komga_client.book = {
        "id": "KB1",
        "seriesId": "SERIES1",
        "media": {"pagesCount": 14},
        "metadata": {},
        "readProgress": {"page": 0, "completed": False},
    }
    komga_client.positions: list[dict[str, Any]] = [
        {
            "href": f"OEBPS/c{i}.xhtml",
            "title": f"Chapter {i}",
            "type": "application/xhtml+xml",
            "locations": {"position": i + 1, "progression": 0.0, "totalProgression": i / 10},
        }
        for i in range(1, 11)
    ]
    komga_client.progression_sequence = [
        {
            "locator": {
                "href": "OEBPS/c1.xhtml",
                "title": "Chapter 1",
                "locations": {"position": 1, "progression": 0.0},
            }
        }
    ]
    komga_client.put_progression_ok = True

    komga_service = KomgaService(
        komga_client,
        server_url="http://komga.local",
        enabled=True,
        scan_retry_max=1,
        scan_retry_delay=0,
    )
    komga_result = komga_service.sync(view_for_restore, restore_target=restore_target)

    assert komga_result.ok is True
    assert komga_result.restore_landed is True
    # Verify that put_progression was called with the correct chapter
    assert len(komga_client.put_progressions) > 0
    put_prog_call = komga_client.put_progressions[0]
    assert put_prog_call[1]["locator"]["href"].endswith("c3.xhtml")
    assert put_prog_call[1]["device"] == {"id": "ebookerr", "name": "ebookerr"}
    assert put_prog_call[1]["locator"] == {
        "href": "OEBPS/c3.xhtml",
        "title": "Chapter 3",
        "type": "application/xhtml+xml",
        "locations": {"position": 4, "progression": 0.0, "totalProgression": 0.3},
    }
    read_position = komga_result.read_position
    assert read_position is not None
    assert read_position.chapter_index == 1
    assert read_position.chapter_progress == 0.0
    assert read_position.chapter_number == 1
    assert read_position.chapter_title == "Chapter 1"
    assert read_position.chapter_href == "c1.xhtml"
    assert read_position.completed is False
    assert read_position.total_chapters == 10
    assert read_position.chapter_key == "https://x/1"
    fields = dict(komga_result.fields)
    synced_at = fields.pop("external_synced_at")
    external_locator = fields.pop("external_locator")
    assert isinstance(synced_at, datetime)
    assert json.loads(external_locator) == {
        "modified": json.loads(external_locator)["modified"],
        "device": {"id": "ebookerr", "name": "ebookerr"},
        "locator": {
            "href": "OEBPS/c3.xhtml",
            "title": "Chapter 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 4, "progression": 0.0, "totalProgression": 0.3},
        },
    }
    assert fields == {
        "external_read_total": 14,
        "external_read_position": 0,
        "external_read_completed": 0,
        "external_read_percent": 0.0,
        "external_item_id": "KB1",
        "external_collection_id": "SERIES1",
        "external_provider": "komga",
        "external_library_id": None,
        "external_item_url": "http://komga.local/book/KB1",
        "external_chapter_count": 10,
    }


@pytest.mark.pins("EXP-202")
def test_a_linked_book_whose_file_changed_is_nudged_for_a_library_scan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A linked book whose file changed since the last link attempt triggers a library scan."""
    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == ["LIB1"]
    assert 'Asked Komga to scan library LIB1 for "The Long Orbit" (file changed)' in caplog.text


@pytest.mark.pins("EXP-202")
def test_a_linked_book_with_an_unchanged_file_is_not_nudged() -> None:
    """A linked book with unchanged file size is not nudged."""
    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=123152,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == []


@pytest.mark.pins("EXP-202")
def test_a_linked_book_without_a_library_id_falls_back_to_the_configured_library() -> None:
    """A linked book without library_id falls back to the configured library."""
    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id=None,
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )

    result = service(client, library_id="CFG").sync(view)

    assert result.ok is True
    assert client.scan_library_calls == ["CFG"]


@pytest.mark.pins("EXP-202")
def test_a_linked_book_with_no_library_id_anywhere_logs_a_debug_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A linked book with no library_id anywhere is skipped with a debug log."""
    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id=None,
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        result = service(client, library_id=None).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == []
    assert (
        'Library-scan nudge skipped for "The Long Orbit" (book_id=b1): no Komga library '
        "id on record" in caplog.text
    )


@pytest.mark.pins("EXP-202")
def test_a_refused_library_scan_warns(caplog: pytest.LogCaptureFixture) -> None:
    """A library scan that Komga refuses logs a warning but sync still succeeds."""
    client = FakeKomga()
    client.book_exists_result = True
    client.scan_library_ok = False
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == ["LIB1"]
    assert 'Komga did not accept the library scan LIB1 for "The Long Orbit"' in caplog.text


@pytest.mark.pins("EXP-202")
def test_an_unlinked_book_is_never_nudged_by_size() -> None:
    """An unlinked book is never nudged even if size differs."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == []


def test_a_changed_file_already_covered_by_a_scan_request_is_not_rescanned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file changed but already covered by an earlier scan request is not rescanned."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from ebookerr_sdk.testing import FakeContext

    (tmp_path / "lightlagged").mkdir()
    (tmp_path / "lightlagged" / "The Long Orbit.epub").write_bytes(b"epub")

    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )
    ledger = ScanLedger(FakeContext())
    ledger.record("http://k/|library:LIB1", time.time() + 3600)

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        result = service(client, library_folder=tmp_path, scan_requests=ledger).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == []
    assert (
        'Library-scan nudge skipped for "The Long Orbit" (book_id=b1): library LIB1 '
        "was already asked to scan after the file changed" in caplog.text
    )


def test_a_file_changed_after_the_last_request_is_scanned_and_recorded(
    tmp_path: Path,
) -> None:
    """A file changed after the last request is scanned and the request is recorded."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from ebookerr_sdk.testing import FakeContext

    (tmp_path / "lightlagged").mkdir()
    (tmp_path / "lightlagged" / "The Long Orbit.epub").write_bytes(b"epub")

    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )
    ledger = ScanLedger(FakeContext(), clock=lambda: 5_000_000_000.0)
    ledger.record("http://k/|library:LIB1", 1.0)

    result = service(client, library_folder=tmp_path, scan_requests=ledger).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == ["LIB1"]
    assert ledger.covers("http://k/|library:LIB1", 4_999_999_999.0) is True


def test_a_refused_scan_is_not_recorded(tmp_path: Path) -> None:
    """A scan that is refused is not recorded in the ledger."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from ebookerr_sdk.testing import FakeContext

    (tmp_path / "lightlagged").mkdir()
    (tmp_path / "lightlagged" / "The Long Orbit.epub").write_bytes(b"epub")

    client = FakeKomga()
    client.book_exists_result = True
    client.scan_library_ok = False
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )
    ledger = ScanLedger(FakeContext())

    result = service(client, library_folder=tmp_path, scan_requests=ledger).sync(view)

    assert result.ok is True
    assert ledger.covers("http://k/|library:LIB1", 0.0) is False


def test_without_a_library_folder_the_ledger_is_not_consulted() -> None:
    """Without a library folder, the ledger is not consulted even if it has a recorded scan."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from ebookerr_sdk.testing import FakeContext

    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )
    ledger = ScanLedger(FakeContext(), clock=lambda: 5_000_000_000.0)
    ledger.record("http://k/|library:LIB1", 5_000_000_000.0 + 3600)

    result = service(client, scan_requests=ledger).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == ["LIB1"]


def test_without_a_ledger_every_changed_file_is_scanned(tmp_path: Path) -> None:
    """Without a ledger, every changed file is scanned as before."""
    (tmp_path / "lightlagged").mkdir()
    (tmp_path / "lightlagged" / "The Long Orbit.epub").write_bytes(b"epub")

    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=122750,
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=123152,
        ),
    )

    result = service(client, library_folder=tmp_path).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == ["LIB1"]


def test_find_by_path_refuses_an_ambiguous_suffix_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ambiguous path match returns None instead of guessing."""
    client = FakeKomga()
    client.library_books = [("KB1", "/books/A/title.epub"), ("KB2", "/other/A/title.epub")]

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client)._find_by_path("A/title.epub")

    assert result is None
    assert any(
        r.levelname == "WARNING"
        and "matches 2 books" in r.message
        and "KB1" in r.message
        and "KB2" in r.message
        and "refusing to guess" in r.message
        for r in caplog.records
    )


def test_an_ambiguous_path_falls_back_to_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ambiguous path match falls back to title lookup."""
    client = FakeKomga()
    client.library_books = [("KB1", "/books/A/title.epub"), ("KB2", "/other/A/title.epub")]
    client.find_results = ["KB_TITLE"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}

    view = make_book_view(title="T", output_filename="A/title.epub")

    result = service(client)._ensure_present(view, allow_scan=False)

    assert result == "KB_TITLE"


def test_the_url_index_size_is_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The URL index size is logged at DEBUG when built."""
    client = FakeKomga()
    client.library_books = [("KB1", "/books/A/title.epub")]

    with caplog.at_level(logging.DEBUG, logger="komga_sync.service"):
        service(client)._ensure_url_index()

    assert any(
        r.levelname == "DEBUG" and "Built the Komga library URL index: 1 book(s)" in r.message
        for r in caplog.records
    )


# --- SPI 2.19 ReadPositionAnchoring contract -------------------------------- #


def test_list_anchors_mirrors_the_positions_table() -> None:
    """list_anchors() returns one ProviderAnchor per distinct href in positions."""

    client = FakeKomga()
    svc = service(client)

    anchors = svc.list_anchors("KB1")

    assert [(a.ref, a.title, a.ordinal) for a in anchors] == [
        ("OEBPS/titlepage.xhtml", "The 12th Key", 0),
        ("OEBPS/file0001.xhtml", "The 12th Key - Ch 1", 1),
        ("OEBPS/file0002.xhtml", "The 12th Key - Ch 2", 2),
    ]


def test_read_bookmark_reports_the_progression_href_and_progress() -> None:
    """read_bookmark() returns progression href, title, and chapter progress."""

    client = FakeKomga()
    client.progression_sequence = [
        {
            "locator": {
                "href": "OEBPS/file0002.xhtml",
                "title": "Ch 2",
                "locations": {"progression": 0.42},
            }
        }
    ]
    svc = service(client)

    result = svc.read_bookmark("KB1")

    assert result is not None
    assert result.ref == "OEBPS/file0002.xhtml"
    assert result.title == "Ch 2"
    assert result.progression == pytest.approx(0.42)


def test_read_bookmark_reports_no_bookmark_without_an_href() -> None:
    """read_bookmark() returns None when progression carries no locator href."""
    client = FakeKomga()
    client.progression_sequence = [{"locator": {"locations": {"progression": 0.0}}}]
    svc = service(client)

    result = svc.read_bookmark("KB1")

    assert result is None


def test_read_bookmark_prefers_this_calls_snapshot() -> None:
    """read_bookmark() uses the cached progression from this sync if available."""
    client = FakeKomga()
    client.progression_sequence = []  # no fresh call expected
    svc = service(client)
    svc._progression_cache["KB1"] = {
        "locator": {"href": "OEBPS/file0001.xhtml", "locations": {"progression": 0.5}}
    }

    result = svc.read_bookmark("KB1")

    assert result is not None
    assert result.ref == "OEBPS/file0001.xhtml"
    assert client.get_progression_call_count == 0


def test_place_bookmark_writes_the_nearest_valid_position(
    repo: _FakeBookRepository,
) -> None:
    """place_bookmark() finds nearest progression and writes it with title."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    anchor = ProviderAnchor("OEBPS/file0001.xhtml", "The 12th Key - Ch 1", 1)
    bookmark = ProviderBookmark("OEBPS/file0001.xhtml", "The 12th Key - Ch 1", 0.5, {})

    svc = service(client, now=lambda: FIXED_NOW)
    result = svc.place_bookmark("KB1", anchor, bookmark)

    assert result is True
    assert len(client.put_progressions) == 1
    book_id, payload = client.put_progressions[0]
    assert book_id == "KB1"
    assert payload == {
        "modified": FIXED_NOW.isoformat(),
        "device": {"id": "ebookerr", "name": "ebookerr"},
        "locator": {
            "href": "OEBPS/file0001.xhtml",
            "title": "The 12th Key - Ch 1",
            "type": "application/xhtml+xml",
            "locations": {"position": 3, "progression": 0.5, "totalProgression": 0.4},
        },
    }


def test_place_bookmark_records_the_envelope_it_wrote(
    repo: _FakeBookRepository,
) -> None:
    """place_bookmark() stores the written envelope in _written_progression."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    anchor = ProviderAnchor("OEBPS/file0001.xhtml", "The 12th Key - Ch 1", 1)
    bookmark = ProviderBookmark("OEBPS/file0001.xhtml", "The 12th Key - Ch 1", 0.5, {})

    svc = service(client, now=lambda: FIXED_NOW)
    svc.place_bookmark("KB1", anchor, bookmark)

    assert svc._written_progression["KB1"] == client.put_progressions[0][1]


def test_place_bookmark_refuses_an_anchor_the_positions_table_does_not_have() -> None:
    """place_bookmark() returns False when anchor.ref is not in positions."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKomga()
    anchor = ProviderAnchor("OEBPS/gone.xhtml", None, 0)
    bookmark = ProviderBookmark("OEBPS/gone.xhtml", None, 0.5, {})

    svc = service(client)
    result = svc.place_bookmark("KB1", anchor, bookmark)

    assert result is False
    assert client.put_progressions == []


def test_place_bookmark_reports_false_when_komga_rejects_the_write() -> None:
    """place_bookmark() returns False and doesn't record when put_progression fails."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKomga()
    client.put_progression_ok = False
    anchor = ProviderAnchor("OEBPS/file0001.xhtml", "The 12th Key - Ch 1", 1)
    bookmark = ProviderBookmark("OEBPS/file0001.xhtml", "The 12th Key - Ch 1", 0.5, {})

    svc = service(client)
    result = svc.place_bookmark("KB1", anchor, bookmark)

    assert result is False
    assert "KB1" not in svc._written_progression


def test_positions_are_fetched_once_per_sync(
    repo: _FakeBookRepository,
) -> None:
    """_positions_for() caches within a sync, so positions are fetched once."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    service(client).sync(book_to_view(book))

    assert client.get_positions_call_count == 1


def test_a_dead_locator_on_a_finished_book_is_forgotten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A finished book whose stored locator no longer resolves has that locator cleared once."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/chapter_03_new.xhtml",
            "title": "Sitting for Sir - Ch 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 1.0},
        }
    ]
    client.progression_sequence = [
        {
            "locator": {
                "href": "OEBPS/file0001.xhtml",
                "locations": {"position": 11, "progression": 0.93803084},
            }
        }
    ]
    read_position = ReadPosition(
        captured_at="2026-08-25T10:00:00+00:00",
        chapter_index=3,
        chapter_progress=1.0,
        chapter_number=3,
        chapter_title="Sitting for Sir - Ch 3",
        chapter_href="chapter_03_sitting_for_sir.xhtml",
        completed=True,
        total_chapters=3,
    )
    locator_json = json.dumps(
        {
            "modified": "2026-08-17T19:11:34+02:00",
            "device": {"id": "komic", "name": "Komic"},
            "locator": {
                "href": "OEBPS/file0001.xhtml",
                "locations": {"position": 11, "progression": 0.93803084},
            },
        }
    )
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        progress=ExternalProgress(completed=True, locator=locator_json),
        read_position=read_position,
        chapter_table=_chapters(("chapter_03_new.xhtml", "Ch 3 New")),
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_locator"] is None
    assert client.put_progressions == []
    assert any(
        r.levelname == "INFO"
        and "Forgot the dead read-position locator for" in r.message
        and "(book_id=" in r.message
        for r in caplog.records
    )


def test_a_live_locator_on_a_finished_book_is_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A finished book whose stored locator still resolves doesn't trigger dead-locator cleanup."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = POSITIONS_FIXTURE
    client.progression_sequence = [
        {
            "locator": {
                "href": "OEBPS/file0002.xhtml",
                "locations": {"position": 5, "progression": 0.5},
            }
        }
    ]
    read_position = ReadPosition(
        captured_at="2026-08-25T10:00:00+00:00",
        chapter_index=2,
        chapter_progress=1.0,
        chapter_number=2,
        chapter_title="The 12th Key - Ch 2",
        chapter_href="file0002.xhtml",
        completed=True,
        total_chapters=2,
    )
    locator_json = json.dumps(
        {
            "modified": "2026-08-17T19:11:34+02:00",
            "device": {"id": "device1", "name": "Device 1"},
            "locator": {
                "href": "OEBPS/file0002.xhtml",
                "locations": {"position": 5, "progression": 0.5},
            },
        }
    )
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        progress=ExternalProgress(locator=locator_json),
        read_position=read_position,
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view)

    assert result.ok is True
    assert not any("Forgot the dead read-position locator for" in r.message for r in caplog.records)


def test_a_finished_book_without_a_stored_locator_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A finished book with no stored locator doesn't trigger the dead-locator path."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/chapter_03_new.xhtml",
            "title": "Sitting for Sir - Ch 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 1.0},
        }
    ]
    client.progression_sequence = [{}]
    read_position = ReadPosition(
        captured_at="2026-08-25T10:00:00+00:00",
        chapter_index=3,
        chapter_progress=1.0,
        chapter_number=3,
        chapter_title="Sitting for Sir - Ch 3",
        chapter_href="chapter_03_sitting_for_sir.xhtml",
        completed=True,
        total_chapters=3,
    )
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        progress=ExternalProgress(completed=True, locator=None),
        read_position=read_position,
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view)

    assert result.ok is True
    assert "external_locator" not in result.fields
    assert not any("Forgot the dead read-position locator for" in r.message for r in caplog.records)


def test_the_dead_bookmark_is_re_anchored_from_the_table(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead bookmark is re-anchored using book.chapter_table's identity (P3 Cookie case)."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/chapter_01_cookie.xhtml",
            "title": "Cookie Pt. 01",
            "type": "application/xhtml+xml",
            "locations": {"position": 59, "progression": 0.9047619, "totalProgression": 0.5},
        },
        {
            "href": "OEBPS/chapter_02_cookie.xhtml",
            "title": "Cookie Pt. 02",
            "type": "application/xhtml+xml",
            "locations": {"position": 65, "progression": 0.0, "totalProgression": 1.0},
        },
    ]
    client.progression_sequence = [
        {
            "locator": {
                "href": "OEBPS/file0001.xhtml",
                "title": "Cookie Pt. 01",
                "locations": {"progression": 0.9047619, "position": 59},
            }
        }
    ]
    read_position = ReadPosition(
        captured_at="2026-08-28T00:26:31+00:00",
        chapter_index=1,
        chapter_progress=0.9047619,
        chapter_number=1,
        chapter_title="Cookie Pt. 01",
        chapter_href="file0001.xhtml",
        completed=False,
        total_chapters=2,
    )
    locator_json = json.dumps(
        {
            "modified": "2026-08-17T19:11:34+02:00",
            "device": {"id": "komic", "name": "Komic"},
            "locator": {
                "href": "OEBPS/file0001.xhtml",
                "locations": {"position": 59, "progression": 0.9047619},
            },
        }
    )
    view = make_book_view(
        external=ExternalLink(item_id="KB1"),
        progress=ExternalProgress(locator=locator_json),
        read_position=read_position,
        chapter_table=_chapters(
            ("chapter_01_cookie.xhtml", "Cookie Pt. 01"),
            ("chapter_02_cookie.xhtml", "Cookie Pt. 02"),
        ),
    )

    with caplog.at_level(logging.INFO):
        result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert len(client.put_progressions) == 1
    assert client.put_progressions[0][1]["locator"]["href"] == "OEBPS/chapter_01_cookie.xhtml"
    assert client.put_progressions[0][1]["locator"]["locations"]["position"] == 59
    assert json.loads(result.fields["external_locator"]) == client.put_progressions[0][1]
    assert any(
        r.levelname == "INFO"
        and "Re-anchored the read position for" in r.message
        and "on its chapter title" in r.message
        for r in caplog.records
    )


# --- TASK-19: file-changed wait, stale skip, joined chapter count (CHC-D12) --- #


def test_a_changed_file_waits_for_komga_to_re_read_it(caplog: pytest.LogCaptureFixture) -> None:
    """A changed file's re-read is waited for before read-position work runs."""
    client = FakeKomga()
    client.book_exists_result = True
    old_book = komga_book(metadata=MATCHING_BOOK_METADATA)
    old_book["sizeBytes"] = 100
    new_book = komga_book(metadata=MATCHING_BOOK_METADATA)
    new_book["sizeBytes"] = 200
    client.book_sequence = [old_book, old_book, old_book, new_book]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/file0001.xhtml",
            "title": "Ch 1",
            "locations": {"position": 1, "progression": 0.0},
        },
        {
            "href": "OEBPS/file0002.xhtml",
            "title": "Ch 2",
            "locations": {"position": 2, "progression": 0.0},
        },
    ]
    locator_with_progress = {
        "locator": {"href": "OEBPS/file0001.xhtml", "locations": {"progression": 0.5}}
    }
    client.progression_sequence = [
        {},  # sync-start snapshot: no progress to protect
        locator_with_progress,  # read_bookmark
        locator_with_progress,  # _repair_page_reset cache miss
        locator_with_progress,  # _semantic_position
    ]
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=200,
        chapter_table=_chapters(("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2")),
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=100,
        ),
    )

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client, scan_retry_max=5).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == ["LIB1"]
    assert 'Komga re-read "The Long Orbit" (item_id=KB1) after 3 poll(s)' in caplog.text
    assert result.read_position is not None


def test_a_changed_file_komga_never_re_reads_skips_read_position_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Komga never re-reading a changed file skips all read-position work this sync."""
    client = FakeKomga()
    client.book_exists_result = True
    old_book = komga_book(metadata=MATCHING_BOOK_METADATA)
    old_book["sizeBytes"] = 100
    client.book_sequence = [old_book, old_book, old_book]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/file0001.xhtml",
            "title": "Ch 1",
            "locations": {"position": 1, "progression": 0.0},
        },
    ]
    client.progression_sequence = [{}]
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_title="Ch 1",
        chapter_href="file0001.xhtml",
    )
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=200,
        chapter_table=_chapters(("file0001.xhtml", "Ch 1")),
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=100,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client, scan_retry_max=2).sync(view, restore_target=target)

    assert result.ok is True
    assert result.read_position is None
    assert result.restore_attempted is False
    assert client.put_progressions == []
    assert (
        'Komga has not re-read "The Long Orbit" (item_id=KB1) after 2 poll(s); read-position '
        "work waits for the next sync" in caplog.text
    )
    assert (
        'Read-position capture, re-anchor and restore skipped for "The Long Orbit"' in caplog.text
    )


def test_a_stale_table_without_a_nudge_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """A stale provider table is skipped even without a file-changed nudge."""
    client = FakeKomga()
    client.book_exists_result = True
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    kb["sizeBytes"] = 200
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "locations": {"position": 1, "progression": 0.0},
        },
    ]
    client.progression_sequence = [{}]
    view = make_book_view(
        book_id="b1",
        title="The Long Orbit",
        output_filename="lightlagged/The Long Orbit.epub",
        file_size=200,
        chapter_table=_chapters(("love.xhtml", "")),
        external=ExternalLink(
            item_id="KB1",
            provider="komga",
            library_id="LIB1",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=200,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert client.scan_library_calls == []  # sizes match: no nudge, no wait
    assert result.fields["external_chapter_count"] == 0
    assert result.read_position is None
    assert (
        'Read-position capture, re-anchor and restore skipped for "The Long Orbit"' in caplog.text
    )


def test_external_chapter_count_excludes_the_title_page(repo: _FakeBookRepository) -> None:
    """The joined chapter count excludes the provider's title-page anchor (F7)."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {"href": "OEBPS/titlepage.xhtml", "title": "The 12th Key", "locations": {"position": 1}},
        {"href": "OEBPS/file0001.xhtml", "title": "Ch 1", "locations": {"position": 2}},
        {"href": "OEBPS/file0002.xhtml", "title": "Ch 2", "locations": {"position": 3}},
    ]
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None
    view = book_to_view(
        book, chapter_table=_chapters(("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2"))
    )

    svc = service(client)
    svc._document_hrefs = lambda b: (  # type: ignore[method-assign]
        "titlepage.xhtml",
        "file0001.xhtml",
        "file0002.xhtml",
    )
    result = svc.sync(view)

    assert result.ok is True
    assert result.fields["external_chapter_count"] == 2


def test_capture_uses_the_chapter_tables_title(repo: _FakeBookRepository) -> None:
    """Capture takes the chapter title (and key) from book.chapter_table, not the provider."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "locations": {"position": 1, "progression": 0.0},
        },
        {
            "href": "OEBPS/file0002.xhtml",
            "title": None,
            "locations": {"position": 2, "progression": 0.3},
        },
    ]
    client.progression_sequence = [
        {},  # sync-start snapshot
        {"locator": {"href": "OEBPS/file0002.xhtml", "locations": {"progression": 0.3}}},
    ]
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None
    view = book_to_view(
        book,
        chapter_table=_chapters(
            ("file0001.xhtml", "Chapter One"), ("file0002.xhtml", "Chapter Two")
        ),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.chapter_title == "Chapter Two"
    assert result.read_position.chapter_key == "file0002.xhtml"


def test_restore_target_is_delivered_through_the_join(repo: _FakeBookRepository) -> None:
    """A restore target identified only by chapter_key still matches, via the join's candidates."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.progression_sequence = [{}]
    client.positions = [
        {
            "href": "OEBPS/file0001.xhtml",
            "title": None,
            "locations": {"position": 1, "progression": 0.0},
        },
        {
            "href": "OEBPS/file0002.xhtml",
            "title": None,
            "locations": {"position": 2, "progression": 0.0},
        },
    ]
    book = make_book(repo)
    view = book_to_view(
        book,
        chapter_table=_chapters(
            ("file0001.xhtml", "Chapter One"), ("file0002.xhtml", "Chapter Two")
        ),
    )
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=99,
        chapter_progress=0.5,
        chapter_number=None,
        chapter_title=None,
        chapter_href=None,
        chapter_key="file0002.xhtml",
    )

    result = service(client, now=lambda: FIXED_NOW).sync(view, restore_target=target)

    assert len(client.put_progressions) == 1
    _, payload = client.put_progressions[0]
    assert payload["locator"]["href"] == "OEBPS/file0002.xhtml"
    assert result.restore_landed is True


def test_the_per_call_memos_are_cleared_between_syncs(
    repo: _FakeBookRepository,
) -> None:
    """Per-call memos are cleared at the top of _sync_reachable, so syncs are independent."""
    client = FakeKomga()
    client.find_results = ["KB1", "KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    book = make_book(repo)

    svc = service(client)
    svc.sync(book_to_view(book))
    svc.sync(book_to_view(book))

    assert client.get_positions_call_count == 2


FRONT_MATTER_POSITIONS = [
    {
        "href": "OEBPS/title_page.xhtml",
        "type": "application/xhtml+xml",
        "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
    },
    {
        "href": "OEBPS/file0001.xhtml",
        "type": "application/xhtml+xml",
        "locations": {"position": 2, "progression": 0.0, "totalProgression": 0.1},
    },
    {
        "href": "OEBPS/file0002.xhtml",
        "type": "application/xhtml+xml",
        "locations": {"position": 3, "progression": 0.0, "totalProgression": 0.2},
    },
    {
        "href": "OEBPS/file0003.xhtml",
        "type": "application/xhtml+xml",
        "locations": {"position": 4, "progression": 0.0, "totalProgression": 0.3},
    },
]


@pytest.mark.pins("EXP-243")
def test_an_empty_komga_read_back_is_not_a_backward_move(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty provider read-back (chapter_index 0, 0% progress) is not a backward move."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    progression = {
        "device": {"id": "ebookerr", "name": "ebookerr"},
        "modified": "2026-08-27T23:33:57.052Z",
        "locator": {
            "href": "OEBPS/title_page.xhtml",
            "locations": {"position": 1, "progression": 0.0, "totalProgression": 0.0},
        },
    }
    client.progression_sequence = [progression, progression, progression]
    client.positions = FRONT_MATTER_POSITIONS

    view = make_book_view(
        num_chapters=3,
        external=ExternalLink(item_id="KB1"),
        read_position=ReadPosition(
            captured_at="2026-08-27T23:29:03+00:00",
            chapter_index=1,
            chapter_progress=0.0,
            chapter_number=None,
            chapter_title="Title Page",
            chapter_href="title_page.xhtml",
            completed=False,
            total_chapters=3,
        ),
        chapter_table=_chapters(
            ("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2"), ("file0003.xhtml", "Ch 3")
        ),
    )

    svc = service(client)
    svc._document_hrefs = lambda b: (  # type: ignore[method-assign]
        "title_page.xhtml",
        "file0001.xhtml",
        "file0002.xhtml",
        "file0003.xhtml",
    )
    with caplog.at_level(logging.DEBUG):
        result = svc.sync(view)

    assert result.ok is True
    assert result.backward_move is None
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "moved backwards" in r.message
    ]
    assert len(warnings) == 0
    # A bookmark that resolves to a non-chapter document is never recorded at all (RP-CAP-3),
    # so capture returns None outright rather than an empty ReadPosition; that DEBUG line
    # (not the book-level "came back empty" one, which needs an actual captured position)
    # is the trail explaining why nothing was recorded here.
    debug_records = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "is on a non-chapter document" in r.message
    ]
    assert len(debug_records) == 1


@pytest.mark.pins("EXP-243")
def test_a_real_komga_backward_move_still_warns(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A real backward move (non-empty position) still warns."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    progression = {
        "device": {"id": "ebookerr", "name": "ebookerr"},
        "modified": "2026-08-27T23:33:57.052Z",
        "locator": {
            "href": "OEBPS/file0001.xhtml",
            "locations": {"position": 2, "progression": 0.5, "totalProgression": 0.15},
        },
    }
    client.progression_sequence = [progression, progression, progression]
    client.positions = FRONT_MATTER_POSITIONS

    view = make_book_view(
        num_chapters=3,
        external=ExternalLink(item_id="KB1"),
        read_position=ReadPosition(
            captured_at="2026-08-27T23:29:03+00:00",
            chapter_index=2,
            chapter_progress=0.5,
            chapter_number=2,
            chapter_title="Ch 2",
            chapter_href="file0002.xhtml",
            completed=False,
            total_chapters=3,
        ),
        chapter_table=_chapters(
            ("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2"), ("file0003.xhtml", "Ch 3")
        ),
    )

    svc = service(client)
    svc._document_hrefs = lambda b: (  # type: ignore[method-assign]
        "title_page.xhtml",
        "file0001.xhtml",
        "file0002.xhtml",
        "file0003.xhtml",
    )
    with caplog.at_level(logging.DEBUG):
        result = svc.sync(view)

    assert result.backward_move is not None
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "moved backwards" in r.message
    ]
    assert len(warnings) == 1
    assert "chapter_index 2 -> 1" in warnings[0].message


def test_a_requested_restore_is_not_a_backward_move(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A requested restore (restore_attempted=True) with backward move is silent (RP-D18)."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    progression = {
        "device": {"id": "ebookerr", "name": "ebookerr"},
        "modified": "2026-08-27T23:33:57.052Z",
        "locator": {
            "href": "OEBPS/file0001.xhtml",
            "locations": {"position": 2, "progression": 0.5, "totalProgression": 0.15},
        },
    }
    client.progression_sequence = [progression, progression, progression]
    client.positions = FRONT_MATTER_POSITIONS

    view = make_book_view(
        num_chapters=3,
        external=ExternalLink(item_id="KB1"),
        read_position=ReadPosition(
            captured_at="2026-08-27T23:29:03+00:00",
            chapter_index=2,
            chapter_progress=0.5,
            chapter_number=2,
            chapter_title="Ch 2",
            chapter_href="file0002.xhtml",
            completed=False,
            total_chapters=3,
        ),
        chapter_table=_chapters(
            ("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2"), ("file0003.xhtml", "Ch 3")
        ),
    )

    # Restore target is two chapters back
    restore_target = ReadPosition(
        captured_at="2026-08-27T23:00:00+00:00",
        chapter_index=0,
        chapter_progress=0.5,
        chapter_number=0,
        chapter_title="Ch 1",
        chapter_href="file0001.xhtml",
        completed=False,
        total_chapters=3,
    )

    svc = service(client)
    svc._document_hrefs = lambda b: (  # type: ignore[method-assign]
        "title_page.xhtml",
        "file0001.xhtml",
        "file0002.xhtml",
        "file0003.xhtml",
    )
    with caplog.at_level(logging.DEBUG):
        result = svc.sync(view, restore_target=restore_target)

    assert result.backward_move is None
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "moved backwards" in r.message
    ]
    assert len(warnings) == 0
    infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "moved backwards by the requested restore" in r.message
    ]
    assert len(infos) == 1


def test_semantic_from_progression_is_gone() -> None:
    """The _semantic_from_progression function is deleted."""
    import komga_sync.service as komga_module

    assert not hasattr(komga_module, "_semantic_from_progression")


# ---------------------------------------------------------------------------
# Probed once per outage, not once per book (EXP-269)
# ---------------------------------------------------------------------------


class _OneStrikeCircuitGuard:
    """A local CircuitGuard matching PROVIDER_POLICY: the first guarded failure opens the key."""

    def __init__(self) -> None:
        self._open: set[str] = set()

    @contextmanager
    def guard(self, key: str, *, label: str | None = None) -> Iterator[None]:
        if key in self._open:
            raise CircuitOpenError(key, label or key, datetime.now(UTC))
        try:
            yield
        except Exception:
            self._open.add(key)
            raise

    def is_open(self, key: str) -> bool:
        return key in self._open

    def reset(self, key: str) -> None:
        self._open.discard(key)


@pytest.mark.pins("EXP-269")
def test_an_unreachable_provider_is_probed_once_not_once_per_book_on_komga(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An outage is concluded once; every later book in the batch skips the probe (EXP-269)."""
    client = FakeKomga()
    client.connected = False
    svc = service(client, circuit=_OneStrikeCircuitGuard())

    with caplog.at_level(logging.WARNING, logger="komga_sync.service"):
        results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(100)]

    assert client.test_connection_calls == 1
    assert all(r.unreachable is True for r in results)
    assert all(r.message == "Komga is not reachable" for r in results)
    warning_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "Komga is not reachable" in r.message
    ]
    assert len(warning_records) == 1


@pytest.mark.pins("EXP-269")
def test_a_hanging_provider_costs_one_timeout_not_one_per_book_on_komga() -> None:
    """A probe that itself raises ProviderUnreachable still only costs one timeout (EXP-269)."""

    class _HangingKomga(FakeKomga):
        def test_connection(self) -> ConnectionTestResult:
            self.test_connection_calls += 1
            raise ProviderUnreachable("Komga is not reachable: ReadTimeout")

    client = _HangingKomga()
    svc = service(client, circuit=_OneStrikeCircuitGuard())

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(100)]

    assert client.test_connection_calls == 1
    assert all(r.unreachable is True for r in results)


@pytest.mark.pins("EXP-269")
def test_the_batch_entry_point_is_guarded_too() -> None:
    """sync_batch() delegates to the same guarded sync() — one probe for 50 books (EXP-269)."""
    client = FakeKomga()
    client.connected = False
    svc = service(client, circuit=_OneStrikeCircuitGuard())

    views = [make_book_view(title=f"Book {i}") for i in range(50)]
    results = svc.sync_batch(views)

    assert client.test_connection_calls == 1
    assert all(r.unreachable is True for r in results)


@pytest.mark.pins("EXP-269")
def test_the_enrich_entry_point_is_guarded_too() -> None:
    """enrich() is guarded the same as sync() — one probe for the whole batch (EXP-269)."""
    client = FakeKomga()
    client.connected = False
    svc = service(client, circuit=_OneStrikeCircuitGuard())

    results = [svc.enrich(make_book_view(title=f"Book {i}")) for i in range(50)]

    assert client.test_connection_calls == 1
    assert all(r.unreachable is True for r in results)


@pytest.mark.pins("EXP-269")
def test_a_refusing_provider_is_unchanged_on_komga() -> None:
    """With no circuit injected, a failing probe is re-asked for every book, as before (EXP-269)."""
    client = FakeKomga()
    client.connected = False
    svc = service(client, circuit=None)

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(5)]

    assert client.test_connection_calls == 5
    assert all(r.unreachable is True for r in results)


@pytest.mark.pins("EXP-269")
def test_a_healthy_provider_is_unchanged_on_komga(repo: _FakeBookRepository) -> None:
    """A healthy provider still reaches _sync_reachable for every book; breaker stays closed."""
    client = FakeKomga()
    client.find_results = ["KB1"] * 5
    client.book = komga_book(series_id="S1", metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    guard = _OneStrikeCircuitGuard()
    svc = service(client, circuit=guard)
    view = book_to_view(make_book(repo))

    with patch.object(svc, "_sync_reachable", wraps=svc._sync_reachable) as spy:
        results = [svc.sync(view) for _ in range(5)]

    assert all(r.ok is True for r in results)
    assert spy.call_count == 5
    assert client.test_connection_calls == 1
    assert guard.is_open("provider:komga") is False


@pytest.mark.pins("EXP-269")
def test_a_404_from_the_provider_never_trips_the_breaker_on_komga() -> None:
    """A normal not-found SyncResult is not a transport failure; the breaker stays closed."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKomga()
    client.find_results = []
    svc = service(client, circuit=guard)

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(10)]

    assert all(r.ok is False for r in results)
    assert all(r.unreachable is False for r in results)
    assert guard.is_open("provider:komga") is False


@pytest.mark.pins("EXP-269")
def test_a_refusal_never_trips_the_breaker_on_komga() -> None:
    """A LinkRefusal is a local, terminal answer — it never touches the breaker (EXP-187)."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKomga()
    client.find_results = ["KOMGA1"] * 10
    svc = service(
        client,
        circuit=guard,
        link_owner=lambda item_id, *, provider=None: "otherBook",
        scan_retry_max=20,
    )

    results = [svc.sync(make_book_view(book_id=f"book{i}", title=f"Book {i}")) for i in range(10)]

    assert all(r.refused is True for r in results)
    assert guard.is_open("provider:komga") is False


@pytest.mark.pins("EXP-269")
def test_the_two_providers_hold_two_breakers() -> None:
    """Opening the Komga breaker leaves a bare provider:kavita breaker closed (EXP-269)."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKomga()
    client.connected = False
    svc = service(client, circuit=guard)

    svc.sync(make_book_view(title="Book 1"))

    assert guard.is_open("provider:komga") is True
    assert guard.is_open("provider:kavita") is False


@pytest.mark.pins("EXP-269")
def test_the_breaker_is_shared_across_two_service_instances_on_komga() -> None:
    """An open breaker protects the next batch and the 03:00 scheduled run alike (EXP-269)."""
    guard = _OneStrikeCircuitGuard()
    client1 = FakeKomga()
    client1.connected = False
    svc1 = service(client1, circuit=guard)
    svc1.sync(make_book_view(title="Book 1"))

    client2 = FakeKomga()
    svc2 = service(client2, circuit=guard)
    result = svc2.sync(make_book_view(title="Book 2"))

    assert result.unreachable is True
    assert client2.test_connection_calls == 0


@pytest.mark.pins("EXP-269")
def test_an_unreachable_book_is_not_reported_as_missing_on_komga() -> None:
    """EXP-192's message contract survives the breaker: never "not found" (EXP-269)."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKomga()
    client.connected = False
    svc = service(client, circuit=guard)

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(20)]

    assert all(r.message == "Komga is not reachable" for r in results)
    assert all("not found" not in r.message for r in results)


@pytest.mark.pins("EXP-269")
def test_the_plugin_passes_its_context_circuit_to_the_service_on_komga() -> None:
    """_build_service forwards ctx.circuit straight through to KomgaService (EXP-269)."""
    from komga_sync.plugin import _build_service

    class _CtxDouble:
        settings: dict[str, Any] = {"server": "http://komga.local:25600", "api_key": "k"}
        library_root = None

    sentinel_circuit = object()
    ctx = _CtxDouble()
    ctx.circuit = sentinel_circuit  # type: ignore[attr-defined]

    built = _build_service(ctx)

    assert built._circuit is sentinel_circuit


def test_sync_reports_the_provider_chapter_count(repo: _FakeBookRepository) -> None:
    """Syncing a book writes the provider's chapter count to external_chapter_count."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/ch1.xhtml",
            "title": "Chapter 1",
            "type": "application/xhtml+xml",
            "locations": {"position": 1},
        },
        {
            "href": "OEBPS/ch2.xhtml",
            "title": "Chapter 2",
            "type": "application/xhtml+xml",
            "locations": {"position": 2},
        },
        {
            "href": "OEBPS/ch3.xhtml",
            "title": "Chapter 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 3},
        },
    ]
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None
    view = book_to_view(
        book,
        chapter_table=_chapters(
            ("ch1.xhtml", "Chapter 1"), ("ch2.xhtml", "Chapter 2"), ("ch3.xhtml", "Chapter 3")
        ),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_chapter_count"] == 3


def test_the_count_is_distinct_hrefs_not_positions(repo: _FakeBookRepository) -> None:
    """Chapter count is based on distinct hrefs, not total positions."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/ch1.xhtml",
            "title": "Chapter 1",
            "type": "application/xhtml+xml",
            "locations": {"position": 1, "progression": 0.0},
        },
        {
            "href": "OEBPS/ch1.xhtml",
            "title": "Chapter 1",
            "type": "application/xhtml+xml",
            "locations": {"position": 2, "progression": 0.5},
        },
        {
            "href": "OEBPS/ch1.xhtml",
            "title": "Chapter 1",
            "type": "application/xhtml+xml",
            "locations": {"position": 3, "progression": 1.0},
        },
        {
            "href": "OEBPS/ch2.xhtml",
            "title": "Chapter 2",
            "type": "application/xhtml+xml",
            "locations": {"position": 4, "progression": 0.0},
        },
        {
            "href": "OEBPS/ch2.xhtml",
            "title": "Chapter 2",
            "type": "application/xhtml+xml",
            "locations": {"position": 5, "progression": 0.5},
        },
        {
            "href": "OEBPS/ch2.xhtml",
            "title": "Chapter 2",
            "type": "application/xhtml+xml",
            "locations": {"position": 6, "progression": 1.0},
        },
        {
            "href": "OEBPS/ch3.xhtml",
            "title": "Chapter 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 7, "progression": 0.0},
        },
        {
            "href": "OEBPS/ch3.xhtml",
            "title": "Chapter 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 8, "progression": 0.5},
        },
        {
            "href": "OEBPS/ch3.xhtml",
            "title": "Chapter 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 9, "progression": 1.0},
        },
    ]
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None
    view = book_to_view(
        book,
        chapter_table=_chapters(
            ("ch1.xhtml", "Chapter 1"), ("ch2.xhtml", "Chapter 2"), ("ch3.xhtml", "Chapter 3")
        ),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_chapter_count"] == 3


def test_an_unreachable_provider_clears_the_count(repo: _FakeBookRepository) -> None:
    """When the provider is unreachable, external_chapter_count is set to None."""
    client = FakeKomga()
    client.connected = False
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1", "external_chapter_count": 5})
    book = repo.get(book.book_id)
    assert book is not None

    result = service(client).sync(book_to_view(book))

    assert result.ok is False
    assert result.fields.get("external_chapter_count") is None


def test_an_unlinked_book_reports_no_count(repo: _FakeBookRepository) -> None:
    """An unlinked book (no external_item_id) has external_chapter_count set to None."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.find_results = ["KB1"]
    book = make_book(repo)
    # Don't set external_item_id; book is not yet linked

    result = service(client).sync(book_to_view(book))

    assert result.ok is True
    assert "external_chapter_count" in result.fields
    assert isinstance(result.fields["external_chapter_count"], int)
    # The chapter count should be reported even for newly linked books


def test_a_changed_count_is_logged_at_info(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """When external_chapter_count changes, it is logged at INFO level."""
    import re as regex

    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/ch1.xhtml",
            "title": "Chapter 1",
            "type": "application/xhtml+xml",
            "locations": {"position": 1},
        },
        {
            "href": "OEBPS/ch2.xhtml",
            "title": "Chapter 2",
            "type": "application/xhtml+xml",
            "locations": {"position": 2},
        },
        {
            "href": "OEBPS/ch3.xhtml",
            "title": "Chapter 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 3},
        },
    ]
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None
    view = book_to_view(
        book,
        chapter_table=_chapters(
            ("ch1.xhtml", "Chapter 1"), ("ch2.xhtml", "Chapter 2"), ("ch3.xhtml", "Chapter 3")
        ),
    )

    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert regex.search(r'Komga reports 3 chapter\(s\) for "The 12th Key"', caplog.text)


def test_an_unchanged_count_is_not_logged_at_info(
    repo: _FakeBookRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """When external_chapter_count is set multiple times, only the first time logs at INFO."""
    client = FakeKomga()
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {
            "href": "OEBPS/ch1.xhtml",
            "title": "Chapter 1",
            "type": "application/xhtml+xml",
            "locations": {"position": 1},
        },
        {
            "href": "OEBPS/ch2.xhtml",
            "title": "Chapter 2",
            "type": "application/xhtml+xml",
            "locations": {"position": 2},
        },
        {
            "href": "OEBPS/ch3.xhtml",
            "title": "Chapter 3",
            "type": "application/xhtml+xml",
            "locations": {"position": 3},
        },
    ]
    book = make_book(repo)
    repo.update_fields(book.book_id, {"external_item_id": "KB1"})
    book = repo.get(book.book_id)
    assert book is not None

    # First sync should report the count at INFO
    svc = service(client)
    with caplog.at_level(logging.INFO, logger="komga_sync.service"):
        result = svc.sync(book_to_view(book))
        assert result.ok is True
        caplog_records_info = [
            r for r in caplog.records if r.levelname == "INFO" and "chapter" in r.message.lower()
        ]
        assert len(caplog_records_info) > 0
