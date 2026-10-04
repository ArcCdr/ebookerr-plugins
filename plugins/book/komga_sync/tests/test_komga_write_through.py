"""Tests for KomgaService.write_position — a reading client's write-through (RDG-D4).

The fakes and builders come from ``test_komga_service``, as in
``test_komga_read_position_restore``: ``service()`` reports the title page and the two chapters
of ``POSITIONS_FIXTURE`` as the file's reading order.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from ebookerr_sdk.providers.connection import ProviderUnreachable
from ebookerr_sdk.spi import BookView, ExternalLink, ReadPosition
from ebookerr_sdk.testing import make_book_view
from test_komga_service import FakeKomga, _chapters, _EchoingKomga, komga_book, service

_ANCHORING_LOGGER = "ebookerr_sdk.providers.anchoring"
_KOMGA_LOGGER = "komga_sync.service"

_TABLE = _chapters(
    ("file0001.xhtml", "The 12th Key - Ch 1"), ("file0002.xhtml", "The 12th Key - Ch 2")
)

_TARGET = ReadPosition(
    captured_at="2026-10-03T12:00:00+00:00",
    chapter_index=2,
    chapter_progress=0.5,
    chapter_title="The 12th Key - Ch 2",
    chapter_href="file0002.xhtml",
    total_chapters=2,
    chapter_key="file0002.xhtml",
)

_READ = {"page": 5, "completed": False, "lastModified": "2026-09-28T18:50:49Z"}


def _view(**overrides: Any) -> BookView:
    """A book linked to Komga item KB1 whose chapter table matches POSITIONS_FIXTURE."""
    fields: dict[str, Any] = {
        "book_id": "b1",
        "title": "The 12th Key",
        "external": ExternalLink(provider="komga", item_id="KB1"),
        "chapter_table": _TABLE,
    }
    fields.update(overrides)
    return make_book_view(**fields)


class _OpenCircuit:
    """A circuit guard whose Komga breaker is open."""

    def is_open(self, key: str) -> bool:
        return True

    def guard(self, key: str, *, label: str | None = None, notice: bool = True) -> Any:
        raise AssertionError("an open breaker is never entered")


def test_a_target_is_written_and_read_back() -> None:
    """Chapter 2 at 0.5: one PUT on chapter 2's locator, the read-back, the envelope persisted."""
    client = _EchoingKomga()
    client.book = komga_book(pages=14, read=_READ)

    status, result = service(client).write_position(_view(), _TARGET)

    assert status == "written"
    assert len(client.put_progressions) == 1
    item_id, payload = client.put_progressions[0]
    assert item_id == "KB1"
    assert payload["locator"]["href"] == "OEBPS/file0002.xhtml"
    assert result is not None
    assert result.fields["external_read_position"] == 5
    assert result.fields["external_progress_modified"] == "2026-09-28T18:50:49+00:00"
    envelope = json.loads(result.fields["external_locator"])
    assert envelope["locator"]["href"] == "OEBPS/file0002.xhtml"
    assert result.read_position is not None
    assert result.read_position.chapter_index == 2


def test_the_restore_success_line_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """The write-through passes quiet=True: the re-anchor success line logs at DEBUG only."""
    client = _EchoingKomga()
    client.book = komga_book(pages=14, read=_READ)

    with caplog.at_level(logging.DEBUG, logger=_ANCHORING_LOGGER):
        service(client).write_position(_view(), _TARGET)

    levels = [
        record.levelno
        for record in caplog.records
        if record.name == _ANCHORING_LOGGER
        and record.getMessage().startswith("Re-anchored the read position for")
    ]
    assert levels == [logging.DEBUG]


def test_the_outcome_is_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """One DEBUG line names the book and the status."""
    client = _EchoingKomga()
    client.book = komga_book(pages=14, read=_READ)

    with caplog.at_level(logging.DEBUG, logger=_KOMGA_LOGGER):
        service(client).write_position(_view(), _TARGET)

    lines = [
        record.levelno
        for record in caplog.records
        if record.name == _KOMGA_LOGGER
        and record.getMessage() == 'Komga write-through for "The 12th Key": written'
    ]
    assert lines == [logging.DEBUG]


def test_a_bookmark_already_at_the_target_is_not_rewritten() -> None:
    """Komga already stands at the target: no PUT, the read-back still comes back."""
    client = FakeKomga()
    client.book = komga_book(pages=14, read=_READ)
    client.progression_sequence = [
        {"locator": {"href": "OEBPS/file0002.xhtml", "locations": {"progression": 0.5}}}
    ]

    status, result = service(client).write_position(_view(), _TARGET)

    assert status == "already"
    assert client.put_progressions == []
    assert result is not None
    assert result.fields["external_read_position"] == 5
    assert "external_locator" not in result.fields


def test_a_chapter_table_komga_does_not_describe_is_stale() -> None:
    """A third chapter Komga has no anchor for: stale, nothing written."""
    client = FakeKomga()
    client.book = komga_book(pages=14, read=_READ)
    table = _chapters(
        ("file0001.xhtml", "The 12th Key - Ch 1"),
        ("file0002.xhtml", "The 12th Key - Ch 2"),
        ("file0003.xhtml", "The 12th Key - Ch 3"),
    )
    svc = service(client)
    svc._document_hrefs = lambda book: (  # type: ignore[method-assign]
        "titlepage.xhtml",
        "file0001.xhtml",
        "file0002.xhtml",
        "file0003.xhtml",
    )

    assert svc.write_position(_view(chapter_table=table), _TARGET) == ("stale", None)
    assert client.put_progressions == []


def test_a_target_komga_has_no_chapter_for_is_unmatched() -> None:
    """No chapter matches the target: unmatched, nothing written."""
    client = FakeKomga()
    client.book = komga_book(pages=14, read=_READ)
    target = ReadPosition(
        captured_at="2026-10-03T12:00:00+00:00",
        chapter_index=99,
        chapter_progress=0.0,
        chapter_title="Nonexistent Chapter",
    )

    assert service(client).write_position(_view(), target) == ("unmatched", None)
    assert client.put_progressions == []


def test_komga_refusing_the_write_is_rejected() -> None:
    """Komga refuses the PUT: rejected, no read-back."""
    client = FakeKomga()
    client.book = komga_book(pages=14, read=_READ)
    client.put_progression_ok = False

    assert service(client).write_position(_view(), _TARGET) == ("rejected", None)
    assert len(client.put_progressions) == 1


def test_a_vanished_komga_book_is_rejected() -> None:
    """Komga no longer has the stored id: rejected."""
    client = FakeKomga()
    client.book = None

    assert service(client).write_position(_view(), _TARGET) == ("rejected", None)
    assert client.put_progressions == []


def test_a_book_without_a_komga_id_is_rejected_without_a_call() -> None:
    """No stored id: rejected, Komga is never asked."""
    client = FakeKomga()

    status, result = service(client).write_position(_view(external=ExternalLink()), _TARGET)

    assert (status, result) == ("rejected", None)
    assert client.test_connection_calls == 0
    assert client.get_book_calls == 0


def test_an_unreachable_komga_raises_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A failed probe raises; the service logs no batch outage warning (the core logs once)."""
    client = FakeKomga()
    client.connected = False

    with (
        caplog.at_level(logging.DEBUG, logger=_KOMGA_LOGGER),
        pytest.raises(ProviderUnreachable, match="connection probe failed"),
    ):
        service(client).write_position(_view(), _TARGET)

    warnings = [
        record
        for record in caplog.records
        if record.name == _KOMGA_LOGGER and record.levelno >= logging.WARNING
    ]
    assert warnings == []


def test_a_disabled_komga_raises() -> None:
    """Komga not configured: the call raises before any request."""
    client = FakeKomga()

    with pytest.raises(ProviderUnreachable, match="Komga is not configured"):
        service(client, enabled=False).write_position(_view(), _TARGET)
    assert client.test_connection_calls == 0


def test_an_open_breaker_raises_without_a_probe() -> None:
    """An open breaker answers from memory: no probe, no request."""
    client = FakeKomga()

    with pytest.raises(ProviderUnreachable, match="circuit open"):
        service(client, circuit=_OpenCircuit()).write_position(_view(), _TARGET)
    assert client.test_connection_calls == 0
    assert client.get_book_calls == 0
