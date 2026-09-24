"""Tests for Kavita read-position restore through the shared anchor join (``CHC-D12``).

Kavita's own TOC entries frequently carry no title at all. Restore no longer reads the
EPUB on disk to fill that gap — the old EPUB-TOC-title fallback (``EXP-194``,
``RP-REST-2``) was retired by ``TASK-21`` along with the ``_page_for_target`` adapter that
carried it. Instead ``KavitaService.sync`` joins Kavita's anchors onto
``book.chapter_table`` — the core's own chapter table, always supplied by the caller —
before any restore is attempted
(:func:`~ebookerr_sdk.providers.anchoring.join_anchors`). These two tests drive that
join end to end through ``service.sync()``, in place of the retired adapter's own
unit tests.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from ebookerr_sdk.spi import ChapterView, ReadPosition
from ebookerr_sdk.testing import make_book_view
from test_kavita_service import FakeKavita, _ref, service

_ANCHORING_LOGGER = "ebookerr_sdk.providers.anchoring"


def _build_toc(titles: list[str | None]) -> list[dict[str, Any]]:
    """Build a Kavita TOC from chapter titles, one page per entry, 1-based."""
    return [{"page": i + 1, "title": title} for i, title in enumerate(titles)]


def _chapters(*titles: str) -> tuple[ChapterView, ...]:
    """Build a ``chapter_table`` from content chapter titles; ordinal from 1."""
    return tuple(
        ChapterView(ordinal=i, key=title, title=title, number="", role="content", href="")
        for i, title in enumerate(titles, start=1)
    )


def test_no_match_still_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    """A target matching nothing still fails closed, logging the core's own warning."""
    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=100)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = _build_toc(["Ch 1", "Ch 2"])
    view = make_book_view(chapter_table=_chapters("Ch 1", "Ch 2"))
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

    with caplog.at_level(logging.WARNING, logger=_ANCHORING_LOGGER):
        result = service(client).sync(view, restore_target=target)

    assert result.ok is True
    assert result.restore_attempted is True
    assert result.restore_landed is False
    assert any("Read-position restore failed" in r.message for r in caplog.records)


def test_backward_move_guard_still_applies() -> None:
    """A title-matched restore still lands even though it moves the reader backwards.

    Restoration is decided by the caller of a pending marker, not by the anchoring core —
    the core never refuses a marker-driven restore just because it looks backward
    (``RP-REST-7``).
    """
    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=100)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 50}
    client.book_chapters_result = _build_toc(["Ch 1", "Ch 2"])
    view = make_book_view(
        chapter_table=_chapters("Ch 1", "Ch 2"),
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.5,
            chapter_title="Ch 2",
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

    result = service(client).sync(view, restore_target=target)

    assert result.restore_landed is True
    assert len(client.save_progress_calls) == 1
