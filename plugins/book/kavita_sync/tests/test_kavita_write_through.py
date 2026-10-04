"""Tests for KavitaService.write_position — a reading client's write-through (RDG-D4).

The fakes and builders come from ``test_kavita_service``. Every chapter of ``_TOC`` is one page
long, as Kavita stores such books: a write lands on the chapter's page and reads back at the
chapter's start.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest
from ebookerr_sdk.providers.connection import ProviderUnreachable
from ebookerr_sdk.spi import BookView, ExternalLink, ExternalProgress, ReadPosition
from ebookerr_sdk.testing import make_book_view
from kavita_sync.client import KavitaRef, KavitaSeriesUnresolved
from test_kavita_service import (
    FakeKavita,
    _chapters,
    _EchoingKavita,
    _ref,
    _RefusingCircuit,
    service,
)

_ANCHORING_LOGGER = "ebookerr_sdk.providers.anchoring"
_KAVITA_LOGGER = "kavita_sync.service"

_TOC: list[dict[str, Any]] = [
    {"title": "Front", "page": 0},
    {"title": "Ch 1", "page": 1},
    {"title": "Ch 2", "page": 2},
    {"title": "Ch 3", "page": 3},
]

_TARGET = ReadPosition(
    captured_at="2026-10-03T12:00:00+00:00",
    chapter_index=2,
    chapter_progress=0.72,
    chapter_title="Ch 2",
    total_chapters=3,
    chapter_key="Ch 2",
)


def _view(**overrides: Any) -> BookView:
    """A book linked to Kavita chapter 11 (series 7, library 1) with a 4-page total."""
    fields: dict[str, Any] = {
        "book_id": "b1",
        "title": "Salt and Circuitry",
        "output_filename": "Corvid/salt_and_circuitry.epub",
        "external": ExternalLink(
            provider="kavita", item_id="11", collection_id="7", library_id="1"
        ),
        "progress": ExternalProgress(position=1, total=4),
        "chapter_table": _chapters("Ch 1", "Ch 2", "Ch 3"),
    }
    fields.update(overrides)
    return make_book_view(**fields)


def _client(cls: type[FakeKavita] = FakeKavita, *, page: int = 1) -> FakeKavita:
    """A Kavita fake that resolves the file to chapter 11 (volume 2) with *page* read."""
    client = cls()
    client.find_chapter_result = _ref(chapter_id=11, total_pages=4)
    client.get_progress_result = {"pageNum": page}
    client.book_chapters_result = _TOC
    return client


class _RefusingKavita(FakeKavita):
    """Refuses every progress write, like a Kavita answering 4xx."""

    def save_progress(self, ref: KavitaRef, page_num: int) -> bool:
        self.save_progress_calls.append((ref, page_num))
        return False


def test_a_target_in_a_one_page_chapter_writes_that_chapters_page() -> None:
    """72 % of a one-page chapter writes that chapter's page and reads back the chapter start."""
    client = _client(_EchoingKavita)

    status, result = service(client).write_position(_view(), _TARGET)

    assert status == "written"
    assert client.save_progress_calls == [(_ref(chapter_id=11, total_pages=4), 2)]
    assert result is not None
    assert result.fields["external_read_position"] == 2
    assert result.read_position is not None
    assert result.read_position.chapter_index == 2
    assert result.read_position.chapter_progress == 0.0


def test_the_write_carries_the_chapters_real_volume_id() -> None:
    """The write goes through find_chapter's ref (volume 2), never the stored ref's volume 0."""
    client = _client(_EchoingKavita)

    service(client).write_position(_view(), _TARGET)

    saved_ref, _ = client.save_progress_calls[0]
    assert saved_ref.volume_id == 2
    assert client.find_chapter_calls == 1


def test_the_restore_success_line_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """The write-through passes quiet=True: the re-anchor success line logs at DEBUG only."""
    client = _client(_EchoingKavita)

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
    client = _client(_EchoingKavita)

    with caplog.at_level(logging.DEBUG, logger=_KAVITA_LOGGER):
        service(client).write_position(_view(), _TARGET)

    lines = [
        record.levelno
        for record in caplog.records
        if record.name == _KAVITA_LOGGER
        and record.getMessage() == 'Kavita write-through for "Salt and Circuitry": written'
    ]
    assert lines == [logging.DEBUG]


def test_a_bookmark_already_at_the_target_is_not_rewritten() -> None:
    """Kavita already stands at the target's page: nothing saved, the read-back comes back."""
    client = _client(page=2)

    status, result = service(client).write_position(_view(), replace(_TARGET, chapter_progress=0.0))

    assert status == "already"
    assert client.save_progress_calls == []
    assert result is not None
    assert result.fields["external_read_position"] == 2


def test_a_file_reindexed_under_another_chapter_is_stale() -> None:
    """Kavita now files the book under chapter 12: stale (the next sync relinks), no write."""
    client = _client()
    client.find_chapter_result = _ref(chapter_id=12, total_pages=4)

    assert service(client).write_position(_view(), _TARGET) == ("stale", None)
    assert client.save_progress_calls == []


def test_a_chapter_table_kavita_does_not_describe_is_stale() -> None:
    """Kavita's TOC lacks chapter 3: stale, nothing written."""
    client = _client()
    client.book_chapters_result = _TOC[:3]

    assert service(client).write_position(_view(), _TARGET) == ("stale", None)
    assert client.save_progress_calls == []


def test_a_file_kavita_no_longer_finds_is_rejected() -> None:
    """find_chapter finds nothing: rejected."""
    client = _client()
    client.find_chapter_result = None

    assert service(client).write_position(_view(), _TARGET) == ("rejected", None)
    assert client.save_progress_calls == []


def test_a_file_whose_series_kavita_cannot_resolve_is_rejected() -> None:
    """find_chapter matched the file but not its series: rejected."""
    client = _client()
    client.find_chapter_result = KavitaSeriesUnresolved("salt_and_circuitry.epub", "no series")

    assert service(client).write_position(_view(), _TARGET) == ("rejected", None)


def test_a_target_kavita_has_no_chapter_for_is_unmatched() -> None:
    """No chapter matches the target: unmatched, nothing written."""
    client = _client()
    target = ReadPosition(
        captured_at="2026-10-03T12:00:00+00:00",
        chapter_index=9,
        chapter_progress=0.0,
        chapter_title="Nope",
    )

    assert service(client).write_position(_view(), target) == ("unmatched", None)
    assert client.save_progress_calls == []


def test_kavita_refusing_the_write_is_rejected() -> None:
    """Kavita refuses the progress write: rejected, no read-back."""
    client = _client(_RefusingKavita)

    assert service(client).write_position(_view(), _TARGET) == ("rejected", None)
    assert len(client.save_progress_calls) == 1


def test_a_book_without_stored_ids_is_rejected_without_a_call() -> None:
    """No stored chapter/series/library ids: rejected, Kavita is never asked."""
    client = _client()

    status, result = service(client).write_position(
        _view(external=ExternalLink(provider="kavita")), _TARGET
    )

    assert (status, result) == ("rejected", None)
    assert client.find_chapter_calls == 0
    assert client.test_connection_calls == 0


def test_an_unreachable_kavita_raises_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A failed probe raises; the service logs no batch outage warning (the core logs once)."""
    client = _client()
    client.connected = False

    with (
        caplog.at_level(logging.DEBUG, logger=_KAVITA_LOGGER),
        pytest.raises(ProviderUnreachable, match="connection probe failed"),
    ):
        service(client).write_position(_view(), _TARGET)

    warnings = [
        record
        for record in caplog.records
        if record.name == _KAVITA_LOGGER and record.levelno >= logging.WARNING
    ]
    assert warnings == []


def test_a_disabled_kavita_raises() -> None:
    """Kavita not configured: the call raises before any request."""
    client = _client()

    with pytest.raises(ProviderUnreachable, match="Kavita is not configured"):
        service(client, enabled=False).write_position(_view(), _TARGET)
    assert client.test_connection_calls == 0


def test_an_open_breaker_raises_without_a_probe() -> None:
    """An open breaker answers from memory: no probe, no lookup."""
    client = _client()

    with pytest.raises(ProviderUnreachable, match="circuit open"):
        service(client, circuit=_RefusingCircuit()).write_position(_view(), _TARGET)
    assert client.test_connection_calls == 0
    assert client.find_chapter_calls == 0
