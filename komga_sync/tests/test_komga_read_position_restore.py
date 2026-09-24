"""Tests for Komga read-position restore using the book's own chapter table (CHC-D12).

Komga's own locator table never carries titles (measured: every locator's ``title`` is
``None``), which used to leave :func:`~ebookerr_sdk.readpos.match_chapter` with
nothing to go on for its first, most durable identity facet. These tests pin the fix:
the join built in :meth:`~src.services.komga_service.KomgaService._sync_reachable`
(``join_anchors``, ``CHC-D12``) now fills that gap from the book's own chapter table
(``BookView.chapter_table``, ``SPI 2.22``/``CHC-D11``) — no EPUB read required for restore
matching itself — and still cooperates with the pre-existing facet order and the
backward-move warning.
"""

from __future__ import annotations

import json
import logging

import pytest
from ebookerr_sdk.spi import BookView, ReadPosition
from ebookerr_sdk.testing import make_book_view
from test_komga_service import (
    MATCHING_BOOK_METADATA,
    MATCHING_SERIES_METADATA,
    FakeKomga,
    _chapters,
    komga_book,
)
from test_komga_service import (
    service as komga_service,
)

_KOMGA_LOGGER = "src.services.komga_service"


def _owner_scenario() -> tuple[BookView, list[dict[str, object]], ReadPosition]:
    """Reconstruct the reported "Cookie 1-5" restore failure from the bug report.

    A Komga table of 5 title-less locators (content chapters only) and a stored target that
    only ever carried the title "Cookie Pt. 01" — the scenario the owner's log line
    (``no chapter matches among 6 candidate(s)``) came from.
    """
    positions = [
        {"href": "chap01.xhtml", "title": None, "locations": {"position": 1, "progression": 0.0}},
        {"href": "chap02.xhtml", "title": None, "locations": {"position": 2, "progression": 0.0}},
        {"href": "chap03.xhtml", "title": None, "locations": {"position": 3, "progression": 0.0}},
        {"href": "chap04.xhtml", "title": None, "locations": {"position": 4, "progression": 0.0}},
        {"href": "chap05.xhtml", "title": None, "locations": {"position": 5, "progression": 0.5}},
    ]
    chapter_table = _chapters(
        ("chap01.xhtml", "Cookie Pt. 01"),
        ("chap02.xhtml", "Cookie Pt. 02"),
        ("chap03.xhtml", "Cookie Pt. 03"),
        ("chap04.xhtml", "Cookie Pt. 04"),
        ("chap05.xhtml", "Cookie Pt. 05"),
    )
    book = make_book_view(
        output_filename="cookie.epub", num_chapters=5, chapter_table=chapter_table
    )
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=2,
        chapter_progress=0.5,
        chapter_number=None,
        chapter_title="Cookie Pt. 01",
        chapter_href=None,
        completed=False,
        total_chapters=4,
    )
    return book, positions, target


def test_owner_scenario_now_matches_on_title() -> None:
    """The reported "Cookie 1-5" failure now matches on the book's own chapter-table title."""
    book, positions, target = _owner_scenario()
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = positions
    client.progression_sequence = [{}]

    result = komga_service(client).sync(book, restore_target=target)

    assert result.restore_landed is True
    assert len(client.put_progressions) == 1
    _, payload = client.put_progressions[0]
    assert payload["locator"]["href"] == "chap01.xhtml"


def test_no_match_still_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    """A target matching nothing in the chapter table still fails closed."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {"href": "file0001.xhtml", "title": None, "locations": {"position": 1, "progression": 0.0}},
        {"href": "file0002.xhtml", "title": None, "locations": {"position": 2, "progression": 0.0}},
    ]
    client.progression_sequence = [{}]
    book = make_book_view(
        output_filename="book.epub",
        chapter_table=_chapters(("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2")),
    )
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=99,
        chapter_progress=0.0,
        chapter_number=None,
        chapter_title="Nonexistent Chapter",
        chapter_href=None,
        completed=False,
        total_chapters=None,
    )

    with caplog.at_level(logging.WARNING, logger=_KOMGA_LOGGER):
        result = komga_service(client).sync(book, restore_target=target)

    assert result.restore_landed is False
    assert any("no chapter matches among" in record.message for record in caplog.records)


def test_backward_move_guard_still_applies() -> None:
    """A title-matched restore that moves the reader backwards still trips EXP-123."""
    client = FakeKomga()
    client.find_results = ["KB1", "KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {"href": "file0001.xhtml", "title": None, "locations": {"position": 1, "progression": 0.0}},
        {"href": "file0002.xhtml", "title": None, "locations": {"position": 2, "progression": 0.0}},
    ]
    chapter_table = _chapters(("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2"))
    book = make_book_view(
        output_filename="book.epub",
        chapter_table=chapter_table,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.5,
            chapter_number=2,
            chapter_title="Ch 2",
            chapter_href="file0002.xhtml",
            completed=False,
            total_chapters=2,
        ),
    )
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.0,
        chapter_number=None,
        chapter_title="Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )
    client.progression_sequence = [{}]

    svc = komga_service(client)
    restore_result = svc.sync(book, restore_target=target)
    assert restore_result.restore_landed is True

    # Simulate the post-restore re-read the sync loop performs, now sitting at the
    # earlier chapter the restore just wrote — every get_progression call this sync sees
    # the same live bookmark (sync-start snapshot, the raw path's re-check, and capture).
    live = {
        "locator": {"href": "file0001.xhtml", "title": "Ch 1", "locations": {"progression": 0.1}}
    }
    client.progression_sequence = [live, live, live]
    result = svc.sync(book)

    assert result.backward_move is not None


def test_restore_semantic_still_returns_the_envelope_on_success() -> None:
    """A successful restore still lands the envelope dict the caller persists."""
    book, positions, target = _owner_scenario()
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = positions
    client.progression_sequence = [{}]

    result = komga_service(client).sync(book, restore_target=target)

    assert result.restore_landed is True
    envelope = json.loads(result.fields["external_locator"])
    assert set(envelope) == {"modified", "device", "locator"}
    assert envelope["locator"]["href"] == "chap01.xhtml"


def test_restore_semantic_still_returns_empty_on_no_match() -> None:
    """A restore with no matching chapter never PUTs and leaves external_locator unset."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {"href": "file0001.xhtml", "title": None, "locations": {"position": 1, "progression": 0.0}},
        {"href": "file0002.xhtml", "title": None, "locations": {"position": 2, "progression": 0.0}},
    ]
    client.progression_sequence = [{}]
    book = make_book_view(
        output_filename="book.epub",
        chapter_table=_chapters(("file0001.xhtml", "Ch 1"), ("file0002.xhtml", "Ch 2")),
    )
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=99,
        chapter_progress=0.0,
        chapter_number=None,
        chapter_title="Nonexistent Chapter",
        chapter_href=None,
        completed=False,
        total_chapters=None,
    )

    result = komga_service(client).sync(book, restore_target=target)

    assert result.restore_landed is False
    assert "external_locator" not in result.fields
    assert client.put_progressions == []


def test_restore_uses_spine_order_not_filename_order() -> None:
    """Chapter-table titles line up by ordinal/spine order, not by filename sort order."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    client.book = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    client.positions = [
        {"href": "file0001.xhtml", "title": None, "locations": {"position": 1, "progression": 0.0}},
        {"href": "file0005.xhtml", "title": None, "locations": {"position": 2, "progression": 0.0}},
        {"href": "file0002.xhtml", "title": None, "locations": {"position": 3, "progression": 0.0}},
    ]
    client.progression_sequence = [{}]
    # Spine order is file0001 -> file0005 -> file0002 (not filename-sorted); the chapter
    # table follows that same spine order (CHC-D9: ordinal is spine position, not sort order).
    book = make_book_view(
        output_filename="book.epub",
        chapter_table=_chapters(
            ("file0001.xhtml", "Ch 1"), ("file0005.xhtml", "Ch 5"), ("file0002.xhtml", "Ch 2")
        ),
    )
    target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=3,
        chapter_progress=0.0,
        chapter_number=None,
        chapter_title="Ch 2",
        chapter_href=None,
        completed=False,
        total_chapters=3,
    )

    result = komga_service(client).sync(book, restore_target=target)

    assert result.restore_landed is True
    _, payload = client.put_progressions[0]
    assert payload["locator"]["href"] == "file0002.xhtml"
