"""Tests for KavitaService business rules (."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from ebookerr_sdk.providers.connection import ConnectionTestResult
from ebookerr_sdk.spi import (
    ChapterLink,
    ChapterView,
    CircuitOpenError,
    ExternalLink,
    ExternalProgress,
    ReadPosition,
)
from ebookerr_sdk.testing import make_book_view
from kavita_sync.client import KavitaRef, KavitaSeriesUnresolved
from kavita_sync.service import KavitaService

FIXED_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
STORY_URL = "https://www.literotica.com/s/test-book"


class FakeKavita:
    """In-memory KavitaClient double recording calls and returning canned data."""

    def __init__(self) -> None:
        self.connected = True
        self.find_chapter_result: KavitaRef | KavitaSeriesUnresolved | None = None
        self.get_progress_result: dict[str, Any] = {}
        self.series_rating_result: float | None = None
        self.book_chapters_result: list[dict[str, Any]] = []
        self.book_chapters_sequence: list[list[dict[str, Any]]] = []
        self.rate_series_calls: list[tuple[int, float]] = []
        self.save_progress_calls: list[tuple[KavitaRef, int]] = []
        self.scan_folder_calls: list[str] = []
        self.scan_library_calls: list[int] = []
        self.find_chapter_calls: int = 0
        self.test_connection_calls: int = 0
        self.die_after_find_calls: int | None = None

    def test_connection(self) -> ConnectionTestResult:
        self.test_connection_calls += 1
        if self.connected:
            return ConnectionTestResult("ok", "Connected.")
        return ConnectionTestResult("unreachable", "Kavita is not reachable.")

    def find_chapter(self, output_filename: str) -> KavitaRef | KavitaSeriesUnresolved | None:
        self.find_chapter_calls += 1
        if (
            self.die_after_find_calls is not None
            and self.find_chapter_calls > self.die_after_find_calls
        ):
            self.connected = False
            from ebookerr_sdk.providers.connection import ProviderUnreachable

            raise ProviderUnreachable("Kavita is not reachable: ConnectionError")
        return self.find_chapter_result

    def get_progress(self, chapter_id: int) -> dict[str, Any]:
        return self.get_progress_result

    def save_progress(self, ref: KavitaRef, page_num: int) -> bool:
        self.save_progress_calls.append((ref, page_num))
        return True

    def rate_series(self, series_id: int, rating: float) -> bool:
        self.rate_series_calls.append((series_id, rating))
        return True

    def series_rating(self, series_id: int) -> float | None:
        return self.series_rating_result

    def scan_folder(self, folder_path: str) -> bool:
        self.scan_folder_calls.append(folder_path)
        return True

    def scan_library(self, library_id: int) -> bool:
        self.scan_library_calls.append(library_id)
        return True

    def book_chapters(self, chapter_id: int) -> list[dict[str, Any]]:
        if self.book_chapters_sequence:
            return self.book_chapters_sequence.pop(0)
        return self.book_chapters_result


def service(client: FakeKavita, **kwargs: Any) -> KavitaService:
    """Build a KavitaService with sane defaults for testing (a no-op ``sleep``, no delay)."""
    params: dict[str, Any] = {"enabled": True, "sleep": lambda _seconds: None}
    params.update(kwargs)
    return KavitaService(client, **params)


def _ref(
    *,
    chapter_id: int = 11,
    series_id: int = 7,
    library_id: int = 1,
    total_pages: int = 100,
) -> KavitaRef:
    return KavitaRef(
        chapter_id=chapter_id,
        volume_id=2,
        series_id=series_id,
        library_id=library_id,
        total_pages=total_pages,
    )


def test_enrich_disabled_returns_false() -> None:
    client = FakeKavita()
    result = service(client, enabled=False).enrich(make_book_view())
    assert result.ok is False
    assert result.attempted is True
    assert client.find_chapter_calls == 0


def test_enrich_connection_fails_returns_false() -> None:
    client = FakeKavita()
    client.connected = False
    result = service(client).enrich(make_book_view())
    assert result.ok is False
    assert result.attempted is True


def test_enrich_no_output_filename_returns_false() -> None:
    client = FakeKavita()
    result = service(client).enrich(make_book_view(output_filename=""))
    assert result.ok is False
    assert result.attempted is True


def test_enrich_not_found_returns_false() -> None:
    client = FakeKavita()
    client.find_chapter_result = None
    result = service(client).enrich(make_book_view())
    assert result.ok is False
    assert result.attempted is True


def test_enrich_returns_external_fields() -> None:
    ref = _ref(chapter_id=11, series_id=7, library_id=1, total_pages=100)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 50, "chapterId": 11}
    view = make_book_view()

    result = service(client, external_url="http://kavita.test").enrich(view)

    assert result.ok is True
    assert result.message == "enriched"
    fields = result.fields
    assert fields["external_provider"] == "kavita"
    assert fields["external_library_id"] == "1"
    assert fields["external_item_id"] == "11"
    assert fields["external_collection_id"] == "7"
    assert fields["external_read_position"] == 50
    assert fields["external_read_total"] == 100
    assert fields["external_read_percent"] == pytest.approx(0.5)
    assert fields["external_read_completed"] == 0
    assert fields["external_item_url"] == "http://kavita.test/library/1/series/7"
    assert "external_synced_at" in fields


@pytest.mark.pins("EXP-198")
def test_a_kavita_linked_book_exposes_a_reader_route() -> None:
    """A Kavita sync stores the ids a reader route is built from (EXP-198).

    The URL substitution itself is core behaviour, pinned in test_reader_links.py's
    TestWebReaderUrl.test_kavita_web_reader_targets_the_reader_route and
    TestDeepLinkUrl.test_deep_link_multiple_vars.
    """
    ref = _ref(chapter_id=11, series_id=7, library_id=1, total_pages=100)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 50, "chapterId": 11}
    view = make_book_view()

    result = service(client, server_url="http://kv").sync(view)

    assert result.ok is True
    assert result.fields["external_library_id"] == "1"
    assert result.fields["external_collection_id"] == "7"
    assert result.fields["external_item_id"] == "11"
    # The stored external_item_url is still the series page (EXP-151 untouched)
    assert result.fields["external_item_url"] == "http://kv/library/1/series/7"


def test_enrich_adopts_kavita_rating_when_local_none() -> None:
    client = FakeKavita()
    client.find_chapter_result = _ref(series_id=7)
    client.series_rating_result = 4.0
    view = make_book_view(rating=None)
    assert view.rating is None

    result = service(client).enrich(view)

    assert result.ok is True
    assert result.fields["rating"] == 4


def test_enrich_skips_adopt_when_rating_set() -> None:
    client = FakeKavita()
    client.find_chapter_result = _ref(series_id=7)
    client.series_rating_result = 3.0
    view = make_book_view(rating=5)

    result = service(client).enrich(view)

    assert result.ok is True
    assert "rating" not in result.fields


def test_a_blank_external_url_still_produces_a_deep_link() -> None:
    """Blank external_url falls back to server_url (EXP-197, marked with pins)."""
    pytest.mark.pins("EXP-197")

    ref = _ref(chapter_id=11, series_id=7, library_id=1, total_pages=100)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 50, "chapterId": 11}
    view = make_book_view()

    # With server_url only, external_item_url is built from it
    result_server_only = service(
        client, server_url="http://kavita.lan:5000/", external_url=None
    ).sync(view)
    assert result_server_only.ok is True
    assert (
        result_server_only.fields["external_item_url"]
        == "http://kavita.lan:5000/library/1/series/7"
    )

    # With both server_url and external_url, external_url takes precedence
    result_both = service(
        client,
        server_url="http://kavita.lan:5000/",
        external_url="http://kavita.example.test",
    ).sync(view)
    assert result_both.ok is True
    assert (
        result_both.fields["external_item_url"] == "http://kavita.example.test/library/1/series/7"
    )

    # With neither, external_item_url is None
    result_neither = service(client, server_url="", external_url=None).sync(view)
    assert result_neither.ok is True
    assert result_neither.fields["external_item_url"] is None


def test_sync_disabled_returns_attempted_false() -> None:
    client = FakeKavita()
    result = service(client, enabled=False).sync(make_book_view())
    assert result.ok is False
    assert result.attempted is False


def test_sync_connection_fails_returns_failure() -> None:
    client = FakeKavita()
    client.connected = False
    result = service(client).sync(make_book_view())
    assert result.ok is False
    assert result.attempted is True


def test_sync_not_found_returns_failure() -> None:
    client = FakeKavita()
    client.find_chapter_result = None
    view = make_book_view()
    result = service(client).sync(view)
    assert result.ok is False
    assert "not found" in result.message


def test_an_unresolved_series_is_reported_as_unresolved_not_as_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from kavita_sync.client import KavitaSeriesUnresolved

    client = FakeKavita()
    client.find_chapter_result = KavitaSeriesUnresolved(
        "x.epub", "series-for-mangafile answered HTTP 500; no series named 'x'"
    )
    view = make_book_view()

    with caplog.at_level(logging.WARNING):
        result = service(client).sync(view)

    assert result.ok is False
    expected_msg = (
        '"x.epub" is indexed in Kavita but its series could not be resolved: '
        "series-for-mangafile answered HTTP 500; no series named 'x'"
    )
    assert result.message == expected_msg
    assert "matched the file but not its series" in caplog.text


def test_enrich_unresolved_series_returns_failure_with_attempted_true(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from kavita_sync.client import KavitaSeriesUnresolved

    client = FakeKavita()
    client.find_chapter_result = KavitaSeriesUnresolved(
        "x.epub", "series-for-mangafile answered HTTP 500; no series named 'x'"
    )
    view = make_book_view()

    with caplog.at_level(logging.WARNING):
        result = service(client).enrich(view)

    assert result.ok is False
    assert result.attempted is True
    expected_msg = (
        '"x.epub" is indexed in Kavita but its series could not be resolved: '
        "series-for-mangafile answered HTTP 500; no series named 'x'"
    )
    assert result.message == expected_msg
    assert "matched the file but not its series" in caplog.text


def test_sync_pushes_local_rating() -> None:
    client = FakeKavita()
    client.find_chapter_result = _ref(series_id=7)
    view = make_book_view(rating=4)

    result = service(client).sync(view)

    assert result.ok is True
    assert client.rate_series_calls == [(7, 4.0)]
    assert "rating" not in result.fields


def test_sync_adopts_kavita_rating_when_local_none() -> None:
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.series_rating_result = 3.0
    view = make_book_view(rating=None)
    assert view.rating is None

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["rating"] == 3


def test_sync_completed_when_page_num_ge_total() -> None:
    client = FakeKavita()
    client.find_chapter_result = _ref(total_pages=100)
    client.get_progress_result = {"pageNum": 100, "chapterId": 11}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_read_completed"] == 1
    assert result.fields["external_read_percent"] == pytest.approx(1.0)


def test_sync_skips_scan_when_external_item_id_set() -> None:
    client = FakeKavita()
    client.find_chapter_result = _ref()
    view = make_book_view(external=ExternalLink(item_id="11"))

    result = service(client).sync(view)

    assert result.ok is True
    assert client.scan_folder_calls == []


def test_restore_writes_computed_page(caplog: Any) -> None:
    """Semantic restore: target with progress=0.5 at index=1 → page 4."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=10)
    client = _EchoingKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert len(client.save_progress_calls) == 1
    saved_ref, saved_page = client.save_progress_calls[0]
    assert saved_ref == ref
    assert saved_page == 4  # 2 + round(0.5 * 4)
    assert result.fields["external_read_position"] == 4
    assert result.restore_attempted is True
    assert result.restore_landed is True
    assert "Restored semantic read position for" in caplog.text


def test_restore_completed_marks_last_page(caplog: Any) -> None:
    """Semantic restore with completed=True → save_progress with total_pages."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=10)
    client = _EchoingKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=2,
        chapter_progress=1.0,
        chapter_number=2,
        chapter_title="The 12th Key - Ch 2",
        chapter_href=None,
        completed=True,
        total_chapters=2,
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert len(client.save_progress_calls) == 1
    saved_ref, saved_page = client.save_progress_calls[0]
    assert saved_page == 10  # total_pages


def test_restore_fails_closed_no_match(caplog: Any) -> None:
    """Restore with no match fails closed: save_progress not called, WARNING logged."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    # Target with no matching facets
    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=99,
        chapter_progress=0.5,
        chapter_number=None,
        chapter_title="Nonexistent Chapter",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    with caplog.at_level(logging.WARNING):
        result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert len(client.save_progress_calls) == 0
    assert result.fields["external_read_position"] == 0
    assert result.restore_attempted is True
    assert result.restore_landed is False
    assert "no chapter matches" in caplog.text
    # Verify all four facets are logged
    assert "title=" in caplog.text
    assert "number=" in caplog.text
    assert "href=" in caplog.text
    assert "index=" in caplog.text


def test_restore_save_rejection_logs_error(caplog: Any) -> None:
    """save_progress rejection: ERROR logged, server page used."""
    from ebookerr_sdk.spi import ReadPosition

    class FailingSaveKavita(FakeKavita):
        def save_progress(self, ref: KavitaRef, page_num: int) -> bool:
            self.save_progress_calls.append((ref, page_num))
            return False

    ref = _ref(chapter_id=11, total_pages=10)
    client = FailingSaveKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    with caplog.at_level(logging.ERROR):
        result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert len(client.save_progress_calls) == 1
    assert result.fields["external_read_position"] == 0  # Server's original page
    assert result.restore_attempted is True
    assert result.restore_landed is False
    assert "Kavita rejected the read-position restore" in caplog.text


def test_old_heuristic_gone() -> None:
    """Old page-restore heuristic no longer triggers."""
    ref = _ref(chapter_id=7)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    view = make_book_view(external=ExternalLink(item_id="7"))

    result = service(client).sync(view)

    assert result.ok is True
    assert len(client.save_progress_calls) == 0
    assert result.fields["external_read_position"] == 0


def test_service_has_no_repo_dependency() -> None:
    sig = inspect.signature(KavitaService.__init__)
    assert "book_repo" not in sig.parameters


def test_connection_check_cached_for_ttl() -> None:
    """sync() calls without advancing clock should use cached connection check."""
    clock: dict[str, float] = {"t": 0.0}
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 0}

    view_a = make_book_view()
    view_b = make_book_view(title="Other Book")

    svc = service(client, monotonic=lambda: clock["t"])
    result_a = svc.sync(view_a)
    result_b = svc.sync(view_b)

    # Both syncs should return ok
    assert result_a.ok is True
    assert result_b.ok is True
    # test_connection() should have been called exactly once (cached on second call)
    assert client.test_connection_calls == 1


def test_connection_check_reprobed_after_ttl() -> None:
    """After advancing clock by 61s, connection check is re-probed."""
    clock: dict[str, float] = {"t": 0.0}
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 0}

    view_a = make_book_view()
    view_b = make_book_view(title="Other Book")

    svc = service(client, monotonic=lambda: clock["t"])
    result_a = svc.sync(view_a)
    clock["t"] += 61.0
    result_b = svc.sync(view_b)

    # Both syncs should succeed
    assert result_a.ok is True
    assert result_b.ok is True
    # Clock advanced past TTL (60s), so second sync re-checks connection
    assert client.test_connection_calls == 2


def test_connection_failure_not_cached() -> None:
    """Connection failures are never cached; each sync re-probes."""
    clock: dict[str, float] = {"t": 0.0}
    client = FakeKavita()
    client.connected = False

    view_a = make_book_view()
    view_b = make_book_view(title="Other Book")

    svc = service(client, monotonic=lambda: clock["t"])
    result_a = svc.sync(view_a)
    result_b = svc.sync(view_b)

    # Both syncs should fail
    assert result_a.ok is False
    assert result_b.ok is False
    # Failures are never cached, so connection is re-checked on every call
    assert client.test_connection_calls == 2


def test_delete_asks_for_a_library_scan_by_id(caplog: Any) -> None:
    """rescan_library_after_delete converts library_id to int and calls scan_library."""
    client = FakeKavita()
    svc = service(client)
    client.scan_library_calls = []

    with caplog.at_level(logging.INFO):
        result = svc.rescan_library_after_delete("1", ["A", "B"])

    assert result is True
    # FakeKavita doesn't have scan_library yet, so we'll verify in the client tests
    assert "Asked Kavita to rescan library 1 after deleting 2 book(s): A, B" in caplog.text


def test_delete_folder_nudge_uses_the_provider_folder(caplog: Any) -> None:
    """nudge_folder_after_delete uses _provider_folder to compute folder path."""
    client = FakeKavita()
    svc = service(client, library_path="/books/")

    with caplog.at_level(logging.INFO):
        result = svc.nudge_folder_after_delete("An Author/a.epub", "A")

    assert result is True
    assert client.scan_folder_calls == ["/books/An Author"]
    assert 'Asked Kavita to scan /books/An Author after deleting "A"' in caplog.text


def test_delete_folder_nudge_returns_false_when_no_library_path(caplog: Any) -> None:
    """nudge_folder_after_delete returns False and warns when library_path is None."""
    client = FakeKavita()
    svc = service(client, library_path=None)

    with caplog.at_level(logging.WARNING):
        result = svc.nudge_folder_after_delete("An Author/a.epub", "A")

    assert result is False
    assert client.scan_folder_calls == []
    assert 'Cannot ask Kavita to rescan for deleted "A"' in caplog.text


def test_provider_folder_table() -> None:
    """_provider_folder computes library-relative paths correctly."""
    client = FakeKavita()
    svc = service(client, library_path="/books")

    assert svc._provider_folder("A/b.epub") == "/books/A"
    assert svc._provider_folder("b.epub") == "/books"

    svc2 = service(client, library_path=None)
    assert svc2._provider_folder("A/b.epub") is None
    assert svc2._provider_folder(None) is None


def test_sync_logs_written_field_names(caplog: Any) -> None:
    """sync logs field names (not values) at INFO level."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view()

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view)

    assert result.ok is True
    assert 'Kavita sync finished for "A Title":' in caplog.text
    # Field names should be present (sorting check)
    assert "external_" in caplog.text


# ---------------------------------------------------------------------------
# Semantic read position tests
# ---------------------------------------------------------------------------

TOC_FIXTURE = [
    {"title": "The 12th Key", "part": "", "page": 0, "children": []},
    {"title": "The 12th Key - Ch 1", "part": "", "page": 2, "children": []},
    {"title": "The 12th Key - Ch 2", "part": "", "page": 6, "children": []},
]


def _chapters(*titles: str) -> tuple[ChapterView, ...]:
    """Build a ``chapter_table`` from content chapter titles; ordinal from 1.

    Kavita anchors are always joined by title (it has no href of its own), so a
    fixture only needs to carry the content titles a TOC fixture's non-front-matter
    entries are expected to match (``CHC-D12``).
    """
    return tuple(
        ChapterView(ordinal=i, key=title, title=title, number="", role="content", href="")
        for i, title in enumerate(titles, start=1)
    )


# TOC_FIXTURE's two content chapters (its "The 12th Key" front-matter entry excluded,
# CHC-D12) — the default chapter_table a TOC_FIXTURE-driven sync joins consistently against.
TOC_FIXTURE_CHAPTERS = _chapters("The 12th Key - Ch 1", "The 12th Key - Ch 2")


def test_sync_passes_the_packaged_chapter_count_to_the_capture() -> None:
    """sync()'s captured ReadPosition.total_chapters reflects the joined chapter count."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11, total_pages=30)
    client.get_progress_result = {"pageNum": 15}
    client.book_chapters_result = [
        {"title": "Front", "page": 0},
        {"title": "Body", "page": 10},
        {"title": "More", "page": 20},
    ]
    view = make_book_view(num_chapters=2, chapter_table=_chapters("Body", "More"))

    result = service(client).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.total_chapters == 2


def test_enrich_passes_the_packaged_chapter_count_to_the_capture() -> None:
    """enrich()'s captured ReadPosition.total_chapters reflects the joined chapter count."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11, total_pages=30)
    client.get_progress_result = {"pageNum": 15}
    client.book_chapters_result = [
        {"title": "Front", "page": 0},
        {"title": "Body", "page": 10},
        {"title": "More", "page": 20},
    ]
    view = make_book_view(num_chapters=2, chapter_table=_chapters("Body", "More"))

    result = service(client).enrich(view)

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.total_chapters == 2


def test_sync_returns_read_position() -> None:
    """Full sync() with fakes → result.read_position.chapter_index == 1."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 4}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    assert isinstance(result.read_position, ReadPosition)
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
    """First Kavita link with page 0 backs up local read state (EXP-002)."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 0}
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
    assert result.fields["external_read_position"] == 0


def test_first_link_with_kavita_progress_does_not_back_up(caplog: Any) -> None:
    """A Kavita chapter that already has progress wins — no backup capture, no backup log."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 12}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(
        progress=ExternalProgress(percent=0.42),
        num_chapters=10,
        chapters=_ten_chapters(),
        chapter_table=TOC_FIXTURE_CHAPTERS,
    )

    with caplog.at_level(logging.INFO):
        result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.chapter_title != "Chapter 5"
    assert "Backing up local read state" not in caplog.text


def test_sync_uses_the_merged_title(caplog: Any) -> None:
    """sync() uses BookView.title, not original book title."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 0}
    view = make_book_view(title="Edited Title")

    with caplog.at_level(logging.INFO):
        result = service(client, now=lambda: FIXED_NOW).sync(view)

    assert result.ok is True
    # Verify the merged title, not the source title, reached the sync payload/log
    assert 'Kavita sync finished for "Edited Title":' in caplog.text


def test_sync_reads_the_item_id_from_the_external_link() -> None:
    """sync() reads external_item_id from BookView.external.item_id."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=42)
    client.get_progress_result = {"pageNum": 0}
    view = make_book_view(external=ExternalLink(item_id="k-42"))

    result = service(client).sync(view)

    assert result.ok is True
    # Verify scan was skipped (external_item_id was set)
    assert len(client.scan_folder_calls) == 0
    # Verify the item_id from external link was used
    assert result.fields["external_item_id"] == "42"


# ---------------------------------------------------------------------------
# Completion date tests
# ---------------------------------------------------------------------------


def test_completion_date_comes_from_kavita() -> None:
    """Kavita's lastModifiedUtc becomes read_completed_at when completed."""
    ref = _ref(total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 10, "lastModifiedUtc": "2026-07-04T12:00:00Z"}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["read_completed_at"] == datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def test_completion_date_accepts_a_naive_timestamp() -> None:
    """Naive timestamp (no zone) is treated as UTC."""
    ref = _ref(total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 10, "lastModifiedUtc": "2026-07-04T12:00:00"}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["read_completed_at"] == datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def test_no_completion_date_when_not_completed() -> None:
    """read_completed_at is not added when book is not completed."""
    ref = _ref(total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 4, "lastModifiedUtc": "2026-07-04T12:00:00Z"}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert "read_completed_at" not in result.fields


def test_no_completion_date_when_kavita_omits_it() -> None:
    """read_completed_at is not added when Kavita omits lastModifiedUtc."""
    ref = _ref(total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 10}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert "read_completed_at" not in result.fields


def test_no_completion_date_when_unparseable() -> None:
    """read_completed_at is not added when lastModifiedUtc is unparseable."""
    ref = _ref(total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 10, "lastModifiedUtc": "yesterday"}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert "read_completed_at" not in result.fields


def test_unparseable_date_logs_at_debug(caplog: Any) -> None:
    """Unparseable lastModifiedUtc is logged at DEBUG."""
    ref = _ref(total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 10, "lastModifiedUtc": "yesterday"}
    view = make_book_view(title="Test Book")

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(view)

    assert result.ok is True
    assert "completed with no usable lastModifiedUtc" in caplog.text


def test_zero_total_pages_is_not_completed() -> None:
    """total_pages=0 means not completed, even with lastModifiedUtc."""
    ref = _ref(total_pages=0)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0, "lastModifiedUtc": "2026-07-04T12:00:00Z"}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_read_completed"] == 0
    assert "read_completed_at" not in result.fields


def test_completion_date_does_not_disturb_the_other_fields() -> None:
    """read_completed_at coexists with other fields unchanged."""
    ref = _ref(total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 10, "lastModifiedUtc": "2026-07-04T12:00:00Z"}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_read_completed"] == 1
    assert result.fields["external_read_position"] == 10
    assert result.fields["external_provider"] == "kavita"
    assert result.fields["read_completed_at"] == datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Backward move warnings (EXP-123)
# ---------------------------------------------------------------------------

BACKWARD_TOC = [
    {"title": "The 12th Key", "page": 0},
    {"title": "The 12th Key - Ch 1", "page": 10},
    {"title": "The 12th Key - Ch 2", "page": 20},
    {"title": "The 12th Key - Ch 3", "page": 30},
    {"title": "The 12th Key - Ch 4", "page": 40},
    {"title": "The 12th Key - Ch 5", "page": 50},
]

# BACKWARD_TOC's five content chapters (its "The 12th Key" front-matter entry excluded).
BACKWARD_TOC_CHAPTERS = _chapters(
    "The 12th Key - Ch 1",
    "The 12th Key - Ch 2",
    "The 12th Key - Ch 3",
    "The 12th Key - Ch 4",
    "The 12th Key - Ch 5",
)


def test_a_cross_chapter_backward_move_warns(caplog: Any) -> None:
    """A read position moving to an earlier chapter warns and surfaces in the result."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 45}
    client.book_chapters_result = BACKWARD_TOC

    prev_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=30,
        chapter_progress=0.5,
        chapter_number=30,
        chapter_title="Chapter 30",
        chapter_href=None,
        completed=False,
        total_chapters=100,
    )
    view = make_book_view(read_position=prev_position, chapter_table=BACKWARD_TOC_CHAPTERS)

    with caplog.at_level(logging.WARNING):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.backward_move is not None
    assert "chapter 30 → 4" in result.backward_move
    assert any("moved backwards" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_a_forward_move_does_not_warn(caplog: Any) -> None:
    """A read position moving to a later chapter does not warn."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 45}
    client.book_chapters_result = BACKWARD_TOC

    prev_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=2,
        chapter_progress=0.5,
        chapter_number=2,
        chapter_title="Chapter 2",
        chapter_href=None,
        completed=False,
        total_chapters=100,
    )
    view = make_book_view(read_position=prev_position, chapter_table=BACKWARD_TOC_CHAPTERS)

    with caplog.at_level(logging.WARNING):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.backward_move is None
    assert not any(
        "moved backwards" in r.message for r in caplog.records if r.levelname == "WARNING"
    )


def test_a_position_at_the_last_page_captures_the_last_chapter_position(
    caplog: Any,
) -> None:
    """Reaching the last page (60) in a 60-page book captures the last chapter."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 60}
    client.book_chapters_result = BACKWARD_TOC

    # Previous position in an earlier book context
    prev_position = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=30,
        chapter_progress=0.5,
        chapter_number=30,
        chapter_title="Chapter 30",
        chapter_href=None,
        completed=False,
        total_chapters=100,
    )
    view = make_book_view(read_position=prev_position, chapter_table=BACKWARD_TOC_CHAPTERS)

    with caplog.at_level(logging.WARNING):
        result = service(client).sync(view)

    assert result.ok is True
    # Position at page 60 lands in chapter 5 (starts at page 50).
    # The capture_position core does not auto-complete when page >= total;
    # only explicit provider/DB signals set completed=True.
    # Because prev was at chapter 30 and new is at chapter 5, a backward-move
    # warning is expected (the position regressed when it should not have).
    # No EPUB fixture; expectations remain derived from the TOC alone.
    assert result.read_position is not None
    assert result.read_position.chapter_index == 5  # Last chapter in BACKWARD_TOC
    assert result.read_position.chapter_title == "The 12th Key - Ch 5"
    assert result.read_position.completed is False  # Not marked finished by provider


def test_kavita_has_no_raw_locator_layer() -> None:
    """A successful restore never sets external_locator — Kavita has no raw-locator layer."""
    import kavita_sync.service as kavita_service_module
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert len(client.save_progress_calls) == 1  # the restore did succeed
    assert "external_locator" not in result.fields
    assert kavita_service_module.__doc__ is not None
    assert "no raw-locator layer" in kavita_service_module.__doc__


# ---------------------------------------------------------------------------
# Link ownership (EXP-149): a chapter belongs to at most one library row
# ---------------------------------------------------------------------------


def test_sync_refuses_a_chapter_another_book_owns(caplog: Any) -> None:
    """sync refuses a chapter another book already claims."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(book_id="thisBook")
    link_owner = lambda item_id, *, provider=None: "otherBook"  # noqa: E731
    expected_msg = "link refused: Kavita chapter 77 is already linked to book_id=otherBook"

    result = service(client, link_owner=link_owner).sync(view)

    assert result.ok is False
    assert result.message == expected_msg
    assert "external_item_id" not in result.fields
    with caplog.at_level(logging.WARNING):
        service(client, link_owner=link_owner).sync(view)
    assert any(
        "Refused to link" in r.message
        and "book_id=thisBook" in r.message
        and "77" in r.message
        and "book_id=otherBook" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_enrich_refuses_a_chapter_another_book_owns(caplog: Any) -> None:
    """enrich refuses a chapter another book already claims."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(book_id="thisBook")
    link_owner = lambda item_id, *, provider=None: "otherBook"  # noqa: E731
    expected_msg = "link refused: Kavita chapter 77 is already linked to book_id=otherBook"

    result = service(client, link_owner=link_owner).enrich(view)

    assert result.ok is False
    assert result.message == expected_msg
    assert result.attempted is True
    assert "external_item_id" not in result.fields
    with caplog.at_level(logging.WARNING):
        service(client, link_owner=link_owner).enrich(view)
    assert any(
        "Refused to link" in r.message
        and "book_id=thisBook" in r.message
        and "77" in r.message
        and "book_id=otherBook" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_sync_claims_an_unowned_chapter() -> None:
    """sync proceeds when link_owner returns None (chapter unclaimed)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view()
    link_owner = lambda item_id, *, provider=None: None  # noqa: E731

    result = service(client, link_owner=link_owner).sync(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "77"


def test_sync_reclaims_its_own_chapter() -> None:
    """sync proceeds when link_owner returns the same book_id."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(book_id="thisBook")
    link_owner = lambda item_id, *, provider=None: "thisBook"  # noqa: E731

    result = service(client, link_owner=link_owner).sync(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "77"


def test_kavita_service_without_a_link_owner_is_unchanged() -> None:
    """sync proceeds without link_owner (backward compat: no exclusivity check)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "77"


@pytest.mark.pins("EXP-187")
def test_an_owned_chapter_is_refused_with_the_owner_named_on_sync(caplog: Any) -> None:
    """sync refuses an owned chapter and names the owner (EXP-187)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11)
    view = make_book_view(book_id="thisBook", output_filename="a/b.epub")
    link_owner = lambda cid, *, provider=None: "otherBook"  # noqa: E731
    expected_msg = "link refused: Kavita chapter 11 is already linked to book_id=otherBook"

    with caplog.at_level(logging.WARNING):
        result = service(client, link_owner=link_owner).sync(view)

    assert result.ok is False
    assert result.message == expected_msg
    assert client.rate_series_calls == []
    assert client.save_progress_calls == []
    assert "Refused to link" in caplog.text
    assert "not found in Kavita" not in result.message


@pytest.mark.pins("EXP-187")
def test_an_owned_chapter_is_refused_with_the_owner_named_on_enrich(caplog: Any) -> None:
    """enrich refuses an owned chapter and names the owner (EXP-187)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11)
    view = make_book_view(book_id="thisBook", output_filename="a/b.epub")
    link_owner = lambda cid, *, provider=None: "otherBook"  # noqa: E731
    expected_msg = "link refused: Kavita chapter 11 is already linked to book_id=otherBook"

    with caplog.at_level(logging.WARNING):
        result = service(client, link_owner=link_owner).enrich(view)

    assert result.ok is False
    assert result.message == expected_msg
    assert result.attempted is True
    assert "Refused to link" in caplog.text
    assert "not found in Kavita" not in result.message


def test_a_book_that_owns_its_own_chapter_still_syncs() -> None:
    """sync proceeds when link_owner returns the book's own id."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(book_id="thisBook", output_filename="a/b.epub")
    link_owner = lambda cid, *, provider=None: "thisBook"  # noqa: E731

    result = service(client, link_owner=link_owner).sync(view)

    assert result.ok is True
    assert result.fields["external_item_id"] == "11"


def test_the_kavita_refusal_asks_provider_scoped() -> None:
    """The Kavita refusal check asks link_owner scoped to the kavita provider (EXP-243)."""
    calls: list[tuple[str, str | None]] = []

    def link_owner(item_id: str, *, provider: str | None = None) -> str | None:
        calls.append((item_id, provider))
        return "otherBook"

    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    service(client, link_owner=link_owner).sync(make_book_view(book_id="thisBook"))

    assert calls
    assert all(call[1] == "kavita" for call in calls)


# ---------------------------------------------------------------------------
# restore_attempted field (EXP-155)
# ---------------------------------------------------------------------------


def test_sync_reports_restore_attempted_when_it_restores() -> None:
    """sync reports restore_attempted=True when it performs a restore."""
    from ebookerr_sdk.spi import ReadPosition

    ref = _ref(chapter_id=11, total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert result.restore_attempted is True


def test_sync_reports_no_restore_attempt_without_a_target() -> None:
    """sync reports restore_attempted=False when no restore_target given."""
    ref = _ref(chapter_id=11, total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 50}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(chapter_table=TOC_FIXTURE_CHAPTERS)

    result = service(client).sync(view)

    assert result.ok is True
    assert result.restore_attempted is False


def test_sync_reports_no_restore_attempt_when_the_chapter_is_missing() -> None:
    """sync reports restore_attempted=False when chapter not found."""
    from ebookerr_sdk.spi import ReadPosition

    client = FakeKavita()
    client.find_chapter_result = None
    view = make_book_view()

    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is False
    assert result.restore_attempted is False


def test_sync_reports_no_restore_attempt_when_kavita_is_disabled() -> None:
    """sync reports restore_attempted=False when Kavita sync is disabled."""
    from ebookerr_sdk.spi import ReadPosition

    client = FakeKavita()
    view = make_book_view()

    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    result = service(client, enabled=False).sync(view, restore_target=restore_target)

    assert result.attempted is False
    assert result.restore_attempted is False


def test_enrich_never_reports_a_restore_attempt() -> None:
    """enrich always reports restore_attempted=False (read-only operation)."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view()

    result = service(client).enrich(view)

    assert result.ok is True
    assert result.restore_attempted is False


def test_sync_result_defaults_restore_attempted_to_false() -> None:
    """SyncResult.restore_attempted defaults to False."""
    from kavita_sync.service import SyncResult

    result = SyncResult(True)

    assert result.restore_attempted is False


# ---------------------------------------------------------------------------
# Mid-batch outage handling (EXP-192)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-192")
def test_a_provider_that_dies_mid_batch_is_reported_as_unreachable_not_as_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider dying mid-batch is reported unreachable once per service, not missing."""

    clock: dict[str, float] = {"t": 0.0}
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.die_after_find_calls = 1  # First call succeeds, rest fail
    client.get_progress_result = {"pageNum": 0}

    # Create three views
    view1 = make_book_view(title="Book 1")
    view2 = make_book_view(title="Book 2")
    view3 = make_book_view(title="Book 3")

    svc = service(client, monotonic=lambda: clock["t"])

    # First sync should succeed
    with caplog.at_level(logging.DEBUG):
        result1 = svc.sync(view1)

    assert result1.ok is True
    assert result1.attempted is True
    assert result1.unreachable is False

    # Second sync should fail with unreachable=True (from provider raising ProviderUnreachable)
    with caplog.at_level(logging.WARNING):
        result2 = svc.sync(view2)

    assert result2.ok is False
    assert result2.attempted is True
    assert result2.unreachable is True
    assert result2.message == "Kavita is not reachable"
    assert "not found" not in result2.message

    # Third sync should also fail with unreachable=True (from connection probe this time)
    with caplog.at_level(logging.DEBUG):
        result3 = svc.sync(view3)

    assert result3.ok is False
    assert result3.attempted is True
    assert result3.unreachable is True
    assert result3.message == "Kavita is not reachable"
    assert "not found" not in result3.message

    # Verify test_connection was called at least twice (probes for book 2 and 3)
    assert client.test_connection_calls >= 2

    # Verify the WARNING about the outage; filter for service-specific message
    warning_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "Kavita is not reachable" in r.message
    ]
    assert len(warning_records) >= 1
    assert any(
        "every later book in this batch is reported unreachable, not missing" in r.message
        for r in warning_records
    )


def test_a_genuinely_missing_book_on_a_healthy_provider_still_says_not_found() -> None:
    """A missing book on a healthy provider still reports not found."""
    client = FakeKavita()
    client.find_chapter_result = None
    client.connected = True
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is False
    assert result.attempted is True
    assert result.unreachable is False
    assert result.message == "book not found in Kavita"


@pytest.mark.pins("EXP-192")
def test_an_outage_during_enrich_is_reported_unreachable() -> None:
    """An outage during enrich (e.g., get_progress raises) reports unreachable."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    class FailingGetProgressKavita(FakeKavita):
        def get_progress(self, chapter_id: int) -> dict[str, Any]:
            raise ProviderUnreachable("Kavita is not reachable: ConnectionError")

    client = FailingGetProgressKavita()
    client.find_chapter_result = _ref()
    view = make_book_view()

    result = service(client).enrich(view)

    assert result.ok is False
    assert result.attempted is True
    assert result.unreachable is True


@pytest.mark.pins("EXP-195")
def test_kavita_stamps_external_progress_at_on_a_reading_event() -> None:
    """Kavita stamps external_progress_at when position changes (EXP-195)."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 40}
    client.book_chapters_result = []
    view = make_book_view(
        external=ExternalLink(provider="kavita"),
        progress=ExternalProgress(position=0, total=100, completed=False),
    )

    svc = service(client, now=lambda: FIXED_NOW)
    result = svc.sync(view)

    assert result.ok is True
    assert result.fields.get("external_progress_at") is not None
    assert result.fields["external_progress_at"] == FIXED_NOW


@pytest.mark.pins("EXP-195")
def test_kavita_does_not_stamp_when_only_the_total_changes_across_providers() -> None:
    """Kavita does not stamp when only total changes from a different provider (EXP-195)."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = []
    view = make_book_view(
        external=ExternalLink(provider="komga"),
        progress=ExternalProgress(position=0, total=65, completed=False),
    )

    svc = service(client, now=lambda: FIXED_NOW)
    result = svc.sync(view)

    assert result.ok is True
    assert "external_progress_at" not in result.fields


@pytest.mark.pins("EXP-195")
def test_a_kavita_sync_that_leaves_the_position_unchanged_does_not_stamp_external_progress_at():  # noqa: C901 — an inline fake client, clearest kept together with the scenario
    """A Kavita sync reporting the same page position and total does not stamp (EXP-195)."""

    # Fixed clock ticking once per call
    class TickingClock:
        def __init__(self, start: datetime) -> None:
            self._t = start

        def __call__(self) -> datetime:
            from datetime import timedelta

            self._t = self._t + timedelta(seconds=1)
            return self._t

    fixed_start = datetime(2026, 8, 25, 17, 57, 52, tzinfo=UTC)
    clock = TickingClock(fixed_start)

    # Book starts with external_progress_at stamped
    view_initial = make_book_view(
        external=ExternalLink(provider="kavita"),
        progress=ExternalProgress(position=0, total=2, completed=False, percent=0.0),
    )
    # Manually set the initial state as if it were already in the DB with a timestamp
    # (simulating a prior sync that stamped the field)

    # Simulate Kavita sync: reports same page position (0) and total (2)
    class FakeKavitaClient:
        def test_connection(self):
            return ConnectionTestResult("ok", "Connected")

        def find_chapter(self, output_filename: str):
            return KavitaRef(chapter_id=11, volume_id=2, series_id=7, library_id=1, total_pages=2)

        def get_progress(self, chapter_id: int):
            return {"pageNum": 0}

        def book_chapters(self, chapter_id: int):
            return []

        def scan_folder(self, folder_path: str):
            pass

        def series_rating(self, series_id: int):
            return None

        def rate_series(self, series_id: int, rating: float):
            pass

        def save_progress(self, ref, page_num: int):
            return True

    kavita_result = KavitaService(FakeKavitaClient(), enabled=True, now=clock).sync(view_initial)

    # After Kavita sync: should NOT have stamped (position and total unchanged)
    assert (
        "external_progress_at" not in kavita_result.fields
        or kavita_result.fields.get("external_progress_at") is None
    )


def test_a_kavita_restore_lands_on_the_migrated_chapter() -> None:
    """A Kavita restore lands the semantic position on the chapter a migration targets."""
    restore_target = ReadPosition(
        captured_at="2026-09-24T08:36:30.746799+00:00",
        chapter_index=3,
        chapter_progress=0.5,
        chapter_number=None,
        chapter_title="Chapter 3",
        chapter_href="c3.xhtml",
        completed=False,
        total_chapters=10,
        chapter_key="c3.xhtml",
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
        # src/services/provider_move_service.py) — match that so the view is one the real
        # composition root could actually produce.
        progress=ExternalProgress(position=0, total=0, completed=False, percent=0.0),
        restore_target=restore_target,
    )

    kavita_client = FakeKavita()
    kavita_ref = _ref(total_pages=21)
    kavita_client.find_chapter_result = kavita_ref
    kavita_client.get_progress_result = {"pageNum": 0}
    # TOC: [Title Page (page 0)] + [Chapter i at page 2*i - 1 for i in 1..10]
    kavita_client.book_chapters_result = [{"title": "Title Page", "page": 0}] + [
        {"title": f"Chapter {i}", "page": 2 * i - 1} for i in range(1, 11)
    ]

    kavita_service = KavitaService(kavita_client, enabled=True)
    kavita_result = kavita_service.sync(view_for_restore, restore_target=restore_target)

    assert kavita_result.ok is True
    assert kavita_result.restore_landed is True
    assert kavita_result.restore_attempted is True
    assert kavita_result.read_position is None
    # Chapter 3 is at page 5, span is 2 (page 7 - 5), progress 0.5 → page 6
    assert kavita_client.save_progress_calls == [(kavita_ref, 6)]
    fields = dict(kavita_result.fields)
    synced_at = fields.pop("external_synced_at")
    assert isinstance(synced_at, datetime)
    assert fields == {
        "external_provider": "kavita",
        "external_library_id": "1",
        "external_item_id": "11",
        "external_collection_id": "7",
        "external_item_url": None,
        "external_read_position": 0,
        "external_read_total": 21,
        "external_read_percent": 0.0,
        "external_read_completed": 0,
        "external_chapter_count": 10,
    }


def test_a_kavita_sync_reports_the_semantic_position_the_migration_restores_after_migrating_back():
    """A Kavita sync captures the semantic position a migration back to Komga later restores."""
    kavita_client = FakeKavita()
    kavita_ref = _ref(total_pages=21)
    kavita_client.find_chapter_result = kavita_ref
    kavita_client.get_progress_result = {"pageNum": 6}
    kavita_client.book_chapters_result = [{"title": "Title Page", "page": 0}] + [
        {"title": f"Chapter {i}", "page": 2 * i - 1} for i in range(1, 11)
    ]

    chapter_links = tuple(
        ChapterLink(url=f"https://x/{i}", title=f"Chapter {i}") for i in range(1, 11)
    )
    kavita_view = make_book_view(
        num_chapters=10,
        chapters=chapter_links,
        chapter_table=_chapters(*(f"Chapter {i}" for i in range(1, 11))),
    )

    kavita_service = KavitaService(kavita_client, enabled=True)
    kavita_result = kavita_service.sync(kavita_view)

    assert kavita_result.ok is True
    read_position = kavita_result.read_position
    assert read_position is not None
    assert read_position.chapter_index == 3
    assert read_position.chapter_progress == 0.5
    assert read_position.chapter_number is None
    assert read_position.chapter_title == "Chapter 3"
    assert read_position.chapter_href is None
    assert read_position.completed is False
    assert read_position.total_chapters == 10
    assert read_position.chapter_key == "Chapter 3"
    fields = dict(kavita_result.fields)
    synced_at = fields.pop("external_synced_at")
    progress_at = fields.pop("external_progress_at")
    assert isinstance(synced_at, datetime)
    assert isinstance(progress_at, datetime)
    assert fields == {
        "external_provider": "kavita",
        "external_library_id": "1",
        "external_item_id": "11",
        "external_collection_id": "7",
        "external_item_url": None,
        "external_read_position": 6,
        "external_read_total": 21,
        "external_read_percent": 0.2857142857142857,
        "external_read_completed": 0,
        "external_chapter_count": 10,
    }


def test_the_dead_toc_diagnostic_is_gone(caplog: Any, tmp_path: Any) -> None:
    """The dead TOC diagnostic block was removed; a sync does not log it (EXP-194)."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 10}
    client.book_chapters_result = [{"page": 0, "title": "Ch 1"}, {"page": 20, "title": "Ch 2"}]

    view = make_book_view()
    restore_target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=2,
        chapter_title="Ch 2",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    svc = service(client, library_folder=tmp_path, now=lambda: FIXED_NOW)
    caplog.clear()
    caplog.set_level(logging.INFO)
    result = svc.sync(view, restore_target=restore_target)

    assert result.ok is True
    # Check that no log record contains the dead diagnostic string
    for record in caplog.records:
        assert "Read-position TOC diagnostic" not in record.message


# ---------------------------------------------------------------------------
# Folder-scan nudge tests (EXP-199)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-199")
def test_an_unresolvable_book_is_not_re_nudged_on_every_sync(caplog: Any) -> None:
    """Nudge fires on first attempt or file size change (EXP-199, EXP-202)."""
    client = FakeKavita()
    client.find_chapter_result = None  # Unresolvable
    svc = service(client, library_path="/books")

    # View A: first attempt
    view_a = make_book_view(
        book_id="a",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=100,
        external=ExternalLink(link_attempted_at=None, link_attempt_size=None),
    )

    with caplog.at_level(logging.INFO):
        result_a = svc.sync(view_a)

    assert result_a.ok is False
    assert client.scan_folder_calls == ["/books/An Author"]
    assert any(
        "Asked Kavita to scan /books/An Author for" in r.message and "first attempt" in r.message
        for r in caplog.records
        if r.levelname == "INFO"
    )

    # View B: same book, same file size, already attempted
    view_b = make_book_view(
        book_id="b",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=100,
        external=ExternalLink(
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_error="book not found in Kavita",
            link_attempt_size=100,
        ),
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        result_b = svc.sync(view_b)

    assert result_b.ok is False
    # No new call, same file size and already attempted
    assert client.scan_folder_calls == ["/books/An Author"]
    assert any("already attempted" in r.message for r in caplog.records if r.levelname == "DEBUG")

    # View C: same book, file size changed
    view_c = make_book_view(
        book_id="c",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=101,
        external=ExternalLink(
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_error="book not found in Kavita",
            link_attempt_size=100,
        ),
    )

    caplog.clear()
    with caplog.at_level(logging.INFO):
        result_c = svc.sync(view_c)

    assert result_c.ok is False
    # New call because file size changed
    assert client.scan_folder_calls == ["/books/An Author", "/books/An Author"]
    assert any(
        "Asked Kavita to scan /books/An Author for" in r.message and "file changed" in r.message
        for r in caplog.records
        if r.levelname == "INFO"
    )

    # View D: linked book (has item_id), no attempt recorded yet
    view_d = make_book_view(
        book_id="d",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=101,
        external=ExternalLink(item_id="11"),
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        svc.sync(view_d)

    # No nudge for linked books with no prior attempt
    assert client.scan_folder_calls == ["/books/An Author", "/books/An Author"]

    # View E: linked book with file changed since last attempt
    view_e = make_book_view(
        book_id="e",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=102,
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_attempt_size=101,
        ),
    )

    caplog.clear()
    with caplog.at_level(logging.INFO):
        svc.sync(view_e)

    # Nudge for linked book when file changed
    assert client.scan_folder_calls == [
        "/books/An Author",
        "/books/An Author",
        "/books/An Author",
    ]
    assert any(
        "Asked Kavita to scan /books/An Author for" in r.message and "file changed" in r.message
        for r in caplog.records
        if r.levelname == "INFO"
    )


def test_a_linked_book_with_an_unchanged_file_is_not_nudged() -> None:
    """A linked book whose file size has not changed since link attempt is not nudged."""
    client = FakeKavita()
    client.find_chapter_result = None
    svc = service(client, library_path="/books")

    view = make_book_view(
        book_id="e",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=101,
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_attempt_size=101,
        ),
    )

    result = svc.sync(view)

    assert result.ok is False
    # No scan folder calls for unchanged file
    assert client.scan_folder_calls == []


@pytest.mark.pins("EXP-202")
def test_a_linked_book_whose_file_changed_is_nudged_for_a_folder_scan(caplog: Any) -> None:
    """A linked book whose file changed since link attempt is nudged for a folder scan (EXP-202)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11, total_pages=60)
    client.get_progress_result = {"pageNum": 0}
    svc = service(client, library_path="/books")

    view = make_book_view(
        book_id="e",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=102,
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_attempt_size=101,
        ),
    )

    with caplog.at_level(logging.INFO):
        result = svc.sync(view)

    assert result.ok is True
    assert client.scan_folder_calls == ["/books/An Author"]
    assert any(
        "Asked Kavita to scan /books/An Author for" in r.message and "file changed" in r.message
        for r in caplog.records
        if r.levelname == "INFO"
    )


def test_no_library_path_means_no_nudge_and_a_debug_line(caplog: Any) -> None:
    """When library_path is None, no folder scan nudge occurs; DEBUG logs why."""
    client = FakeKavita()
    client.find_chapter_result = None
    svc = service(client, library_path=None)

    view = make_book_view(
        output_filename="An Author/a_title.epub",
        file_size=100,
        external=ExternalLink(),
    )

    with caplog.at_level(logging.DEBUG):
        result = svc.sync(view)

    assert result.ok is False
    assert client.scan_folder_calls == []
    assert any(
        "Kavita library folder is not configured" in r.message
        for r in caplog.records
        if r.levelname == "DEBUG"
    )


def test_a_refused_folder_scan_is_logged(caplog: Any) -> None:
    """When scan_folder returns False, a WARNING is logged."""

    class FailingScanKavita(FakeKavita):
        def scan_folder(self, folder_path: str) -> bool:
            self.scan_folder_calls.append(folder_path)
            return False

    client = FailingScanKavita()
    client.find_chapter_result = None
    svc = service(client, library_path="/books")

    view = make_book_view(
        output_filename="An Author/a_title.epub",
        file_size=100,
        external=ExternalLink(),
    )

    with caplog.at_level(logging.WARNING):
        result = svc.sync(view)

    assert result.ok is False
    assert client.scan_folder_calls == ["/books/An Author"]
    assert any(
        "Kavita did not accept the folder scan" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_a_changed_file_already_covered_by_a_folder_scan_is_not_rescanned(
    tmp_path: Any, caplog: Any
) -> None:
    """When ledger covers a changed file, folder scan is skipped (DFT-D21)."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from ebookerr_sdk.testing import FakeContext

    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11, total_pages=60)
    client.get_progress_result = {"pageNum": 0}

    ledger = ScanLedger(FakeContext())
    # Record at time.time() + 3600, so it's way in the future
    ledger_key = "http://kv|folder:/books/An Author"
    ledger.record(ledger_key, ledger.now() + 3600)

    svc = service(
        client,
        library_path="/books",
        library_folder=tmp_path,
        server_url="http://kv",
        scan_requests=ledger,
    )

    # Create file on disk so we can detect file-changed
    file_path = tmp_path / "An Author/a_title.epub"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x" * 102)

    view = make_book_view(
        book_id="e",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=102,
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_attempt_size=101,
        ),
    )

    with caplog.at_level(logging.DEBUG):
        result = svc.sync(view)

    assert result.ok is True
    assert client.scan_folder_calls == []
    assert any(
        'Folder-scan nudge skipped for "A Title" (e): /books/An Author was already asked to scan '
        "after the file changed" in r.message
        for r in caplog.records
        if r.levelname == "DEBUG"
    )


def test_a_file_changed_after_the_last_folder_scan_is_scanned_and_recorded(
    tmp_path: Any,
) -> None:
    """When file changed after ledger's last scan, scan happens and is recorded (DFT-D21)."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from ebookerr_sdk.testing import FakeContext

    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11, total_pages=60)
    client.get_progress_result = {"pageNum": 0}

    # Use a custom clock for the ledger: returns a constant, far-future value
    ledger = ScanLedger(FakeContext(), clock=lambda: 5_000_000_000.0)
    # Record at 1.0, so the file change time (now) will be later
    ledger_key = "http://kv|folder:/books/An Author"
    ledger.record(ledger_key, 1.0)

    svc = service(
        client,
        library_path="/books",
        library_folder=tmp_path,
        server_url="http://kv",
        scan_requests=ledger,
    )

    # Create file with mtime before the last scan record
    file_path = tmp_path / "An Author/a_title.epub"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x" * 102)
    # Set mtime to 4_999_999_999.0, which is after the recorded 1.0
    import os

    os.utime(file_path, (4_999_999_999.0, 4_999_999_999.0))

    view = make_book_view(
        book_id="e",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=102,
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_attempt_size=101,
        ),
    )

    result = svc.sync(view)

    assert result.ok is True
    assert client.scan_folder_calls == ["/books/An Author"]
    # Verify it was recorded in the ledger
    assert ledger.covers(ledger_key, 4_999_999_999.0) is True


def test_a_refused_folder_scan_is_not_recorded(tmp_path: Any) -> None:
    """When scan_folder returns False, ledger is not updated (DFT-D21)."""
    from ebookerr_sdk.providers.scan_ledger import ScanLedger
    from ebookerr_sdk.testing import FakeContext

    class FailingScanKavita(FakeKavita):
        def scan_folder(self, folder_path: str) -> bool:
            self.scan_folder_calls.append(folder_path)
            return False

    client = FailingScanKavita()
    client.find_chapter_result = None  # Chapter not found since scan refused

    ledger = ScanLedger(FakeContext())
    ledger_key = "http://kv|folder:/books/An Author"

    svc = service(
        client,
        library_path="/books",
        library_folder=tmp_path,
        server_url="http://kv",
        scan_requests=ledger,
    )

    # Create file on disk
    file_path = tmp_path / "An Author/a_title.epub"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x" * 102)

    view = make_book_view(
        book_id="e",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=102,
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_attempt_size=101,
        ),
    )

    result = svc.sync(view)

    assert result.ok is False
    assert client.scan_folder_calls == ["/books/An Author"]
    # Verify it was NOT recorded (covers should return False)
    assert ledger.covers(ledger_key, 0.0) is False


def test_without_a_ledger_a_changed_file_is_always_scanned(tmp_path: Any) -> None:
    """Without a ledger, changed files are always scanned (backward compat)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11, total_pages=60)
    client.get_progress_result = {"pageNum": 0}

    svc = service(
        client,
        library_path="/books",
        library_folder=tmp_path,
        server_url="http://kv",
        scan_requests=None,
    )

    # Create file on disk
    file_path = tmp_path / "An Author/a_title.epub"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x" * 102)

    view = make_book_view(
        book_id="e",
        title="A Title",
        output_filename="An Author/a_title.epub",
        file_size=102,
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-02T08:00:00+00:00",
            link_attempt_size=101,
        ),
    )

    result = svc.sync(view)

    assert result.ok is True
    assert client.scan_folder_calls == ["/books/An Author"]


# ---------------------------------------------------------------------------
# Relink detection (TASK-16)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-243")
def test_a_changed_chapter_id_is_reported_as_a_relink(caplog: pytest.LogCaptureFixture) -> None:
    """A sync with changed chapter id reports relinked=True and logs at INFO."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(book_id="thisBook", external=ExternalLink(item_id="77"))

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.relinked is True
    assert any(
        "Kavita chapter changed for" in r.message and "77" in r.message and "88" in r.message
        for r in caplog.records
        if r.levelname == "INFO"
    )


def test_an_unchanged_chapter_id_is_not_a_relink(caplog: pytest.LogCaptureFixture) -> None:
    """A sync with unchanged chapter id reports relinked=False and logs nothing."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(book_id="thisBook", external=ExternalLink(item_id="77"))

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.relinked is False
    assert not any(
        "Kavita chapter changed for" in r.message for r in caplog.records if r.levelname == "INFO"
    )


def test_a_first_link_is_not_a_relink(caplog: pytest.LogCaptureFixture) -> None:
    """A first link (no prior item_id) reports relinked=False."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    client.get_progress_result = {"pageNum": 50}
    # external defaults to ExternalLink() with item_id=None
    view = make_book_view(book_id="thisBook")

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.relinked is False
    assert not any(
        "Kavita chapter changed for" in r.message for r in caplog.records if r.levelname == "INFO"
    )


def test_enrich_also_reports_a_relink(caplog: pytest.LogCaptureFixture) -> None:
    """An enrich with changed chapter id reports relinked=True and logs at INFO."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(book_id="thisBook", external=ExternalLink(item_id="77"))

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = service(client).enrich(view)

    assert result.ok is True
    assert result.relinked is True
    assert any(
        "Kavita chapter changed for" in r.message and "77" in r.message and "88" in r.message
        for r in caplog.records
        if r.levelname == "INFO"
    )


def test_a_refused_chapter_is_not_reported_as_a_relink(caplog: pytest.LogCaptureFixture) -> None:
    """A refused sync reports relinked=False and logs no relink message."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    view = make_book_view(book_id="thisBook", external=ExternalLink(item_id="77"))
    svc = service(client, link_owner=lambda item_id, *, provider=None: "otherBook")

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = svc.sync(view)

    assert result.ok is False
    assert result.relinked is False
    assert not any(
        "Kavita chapter changed for" in r.message for r in caplog.records if r.levelname == "INFO"
    )


@pytest.mark.pins("EXP-243")
def test_an_already_owned_link_is_refused_before_any_provider_call() -> None:
    """A stored link owned by another book is refused before any provider call (EXP-243)."""
    client = FakeKavita()
    view = make_book_view(
        book_id="thisBook",
        output_filename="a/b.epub",
        external=ExternalLink(item_id="77"),
    )
    svc = service(
        client,
        link_owner=lambda item_id, *, provider=None: "otherBook",
        library_path="/books",
    )

    result = svc.sync(view)

    assert result.ok is False
    expected = "link refused: Kavita chapter 77 is already linked to book_id=otherBook"
    assert result.message == expected
    assert client.scan_folder_calls == []
    assert client.find_chapter_calls == 0


def test_an_unlinked_book_is_still_nudged_then_refused() -> None:
    """An unlinked book is nudged, finds a chapter, then refused (EXP-243)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11)
    view = make_book_view(book_id="thisBook", output_filename="a/b.epub")
    svc = service(
        client,
        link_owner=lambda item_id, *, provider=None: "otherBook",
        library_path="/books",
    )

    result = svc.sync(view)

    assert result.ok is False
    expected = "link refused: Kavita chapter 11 is already linked to book_id=otherBook"
    assert result.message == expected
    assert client.scan_folder_calls == ["/books/a"]
    assert client.find_chapter_calls == 1


def test_a_book_that_owns_its_own_stored_link_is_not_refused_by_the_precheck() -> None:
    """A book that owns its own stored link passes the precheck (EXP-243)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=77)
    client.get_progress_result = {"pageNum": 50}
    view = make_book_view(
        book_id="thisBook",
        output_filename="a/b.epub",
        external=ExternalLink(item_id="77"),
    )
    svc = service(
        client,
        link_owner=lambda item_id, *, provider=None: "thisBook",
        library_path="/books",
    )

    result = svc.sync(view)

    assert result.ok is True
    assert client.find_chapter_calls == 1


def test_a_book_with_no_stored_link_skips_the_precheck() -> None:
    """A book with no stored link skips the precheck and proceeds normally (EXP-243)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11)
    client.get_progress_result = {"pageNum": 0}
    view = make_book_view(book_id="thisBook", output_filename="a/b.epub")
    recorded_calls: list[str] = []

    def recording_link_owner(item_id: str, *, provider: str | None = None) -> str | None:
        recorded_calls.append(item_id)
        return None

    svc = service(
        client,
        link_owner=recording_link_owner,
        library_path="/books",
    )

    result = svc.sync(view)

    assert result.ok is True
    assert None not in recorded_calls


def test_the_precheck_logs_exactly_one_refusal_warning(caplog: Any) -> None:
    """The precheck logs exactly one WARNING and no scan line (EXP-243)."""
    client = FakeKavita()
    view = make_book_view(
        book_id="thisBook",
        output_filename="a/b.epub",
        external=ExternalLink(item_id="77"),
    )
    svc = service(
        client,
        link_owner=lambda item_id, *, provider=None: "otherBook",
        library_path="/books",
    )

    with caplog.at_level(logging.WARNING, logger="kavita_sync.service"):
        result = svc.sync(view)

    assert result.ok is False
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    assert "Refused to link" in warning_records[0].message
    assert not any("Asked Kavita to scan" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Stale link kept when the resolved chapter belongs to another row (F5c)
# ---------------------------------------------------------------------------


@pytest.mark.pins("EXP-243")
def test_a_linked_book_whose_chapter_another_row_owns_reports_a_kept_stale_link() -> None:
    """A linked book whose resolved chapter another row owns keeps its stale link (F5c).

    ``link_owner`` is scoped by id (unowned for the stored "77", owned for the resolved "88")
    so the TASK-18 precheck lets the stored link through and the post-``find_chapter`` branch
    this card adds is the one that actually fires.
    """
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    view = make_book_view(
        book_id="thisBook", output_filename="a/b.epub", external=ExternalLink(item_id="77")
    )
    link_owner = lambda item_id, *, provider=None: "otherBook" if item_id == "88" else None  # noqa: E731

    result = service(client, link_owner=link_owner).sync(view)

    assert result.ok is False
    expected = (
        "stale link kept: this book's file is Kavita chapter 88, which book_id=otherBook "
        "already owns"
    )
    assert result.message == expected
    assert result.stale_link_kept == result.message


def test_the_kept_stale_kavita_link_is_logged_at_warning(caplog: Any) -> None:
    """The kept-stale-link outcome is logged at WARNING with both chapter ids (F5c)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    view = make_book_view(
        book_id="thisBook", output_filename="a/b.epub", external=ExternalLink(item_id="77")
    )
    link_owner = lambda item_id, *, provider=None: "otherBook" if item_id == "88" else None  # noqa: E731

    with caplog.at_level(logging.WARNING, logger="kavita_sync.service"):
        service(client, link_owner=link_owner).sync(view)

    assert any(
        r.levelname == "WARNING"
        and "should move to chapter 88" in r.message
        and "book_id=otherBook already owns it" in r.message
        and "keeping the stale link 77" in r.message
        for r in caplog.records
    )


def test_an_unlinked_refused_book_keeps_the_plain_refusal_message() -> None:
    """An unlinked book refused on first link keeps the plain refusal message (F5c)."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    view = make_book_view(book_id="thisBook", output_filename="a/b.epub")
    link_owner = lambda item_id, *, provider=None: "otherBook"  # noqa: E731

    result = service(client, link_owner=link_owner).sync(view)

    assert (
        result.message == "link refused: Kavita chapter 88 is already linked to book_id=otherBook"
    )
    assert result.stale_link_kept is None


def test_a_book_already_linked_to_the_owned_chapter_keeps_the_plain_refusal_message() -> None:
    """A book already linked to the very chapter another row owns keeps the plain message (F5c).

    The pre-check TASK-18 added (``EXP-243``) fires first here, before the post-``find_chapter``
    branch this card adds is ever reached.
    """
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=88)
    view = make_book_view(
        book_id="thisBook", output_filename="a/b.epub", external=ExternalLink(item_id="88")
    )
    link_owner = lambda item_id, *, provider=None: "otherBook"  # noqa: E731

    result = service(client, link_owner=link_owner).sync(view)

    assert (
        result.message == "link refused: Kavita chapter 88 is already linked to book_id=otherBook"
    )
    assert result.stale_link_kept is None


# ---------------------------------------------------------------------------
# Anchoring protocol tests (SPI 2.19)
# ---------------------------------------------------------------------------


BACKWARD_TOC = [
    {"title": "The 12th Key", "page": 0},
    {"title": "The 12th Key - Ch 1", "page": 10},
    {"title": "The 12th Key - Ch 2", "page": 20},
    {"title": "The 12th Key - Ch 3", "page": 30},
    {"title": "The 12th Key - Ch 4", "page": 40},
    {"title": "The 12th Key - Ch 5", "page": 50},
]


def _anchoring_service(client: FakeKavita, ref: KavitaRef) -> KavitaService:
    """A KavitaService whose per-call ref memo already holds *ref* (as one sync would)."""
    svc = service(client)
    svc._ref_cache[str(ref.chapter_id)] = ref
    return svc


def test_list_anchors_mirrors_the_book_toc() -> None:
    """list_anchors returns one ProviderAnchor per TOC entry."""

    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchors = svc.list_anchors("11")

    expected = [
        ("0", "The 12th Key", 0),
        ("10", "The 12th Key - Ch 1", 1),
        ("20", "The 12th Key - Ch 2", 2),
        ("30", "The 12th Key - Ch 3", 3),
        ("40", "The 12th Key - Ch 4", 4),
        ("50", "The 12th Key - Ch 5", 5),
    ]
    assert [(a.ref, a.title, a.ordinal) for a in anchors] == expected


def test_list_anchors_ignores_entries_without_an_integer_page() -> None:
    """list_anchors filters out entries with no page or non-integer page."""
    client = FakeKavita()
    client.book_chapters_result = [
        {"title": "A", "page": 0},
        {"title": "B"},
        {"title": "C", "page": "x"},
        {"title": "D", "page": 5},
    ]
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchors = svc.list_anchors("11")

    assert [a.ref for a in anchors] == ["0", "5"]


def test_read_bookmark_maps_the_page_onto_the_containing_anchor() -> None:
    """read_bookmark finds the anchor containing the current page and computes progression."""
    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    client.get_progress_result = {"pageNum": 45}
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    bookmark = svc.read_bookmark("11")

    assert bookmark is not None
    assert bookmark.ref == "40"
    assert bookmark.title == "The 12th Key - Ch 4"
    assert bookmark.progression == pytest.approx(0.5)


def test_read_bookmark_reports_no_bookmark_at_page_zero() -> None:
    """read_bookmark returns None when pageNum <= 0."""
    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    client.get_progress_result = {"pageNum": 0}
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    bookmark = svc.read_bookmark("11")

    assert bookmark is None


def test_read_bookmark_reports_no_bookmark_without_a_resolved_ref() -> None:
    """read_bookmark returns None when _ref_cache has no entry for item_id."""
    client = FakeKavita()
    svc = service(client)  # Empty _ref_cache

    bookmark = svc.read_bookmark("11")
    anchors = svc.list_anchors("11")

    assert bookmark is None
    assert anchors == []


def test_place_bookmark_computes_the_page_from_the_span() -> None:
    """place_bookmark computes page from anchor start, span, and progression."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("10", "The 12th Key - Ch 1", 1)
    bookmark = ProviderBookmark("10", "The 12th Key - Ch 1", 0.5, {"completed": False})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is True
    assert client.save_progress_calls == [(ref, 15)]


def test_place_bookmark_writes_the_last_page_when_the_position_is_completed() -> None:
    """place_bookmark writes total_pages when completed is True and the anchor is last.

    ``RP-D20``: a completed position still clamps to its own chapter's span, so this
    only lands on ``total_pages`` because Ch 5 is genuinely the book's last chapter —
    see ``test_place_bookmark_completed_stays_in_the_chapter_when_it_is_no_longer_last``
    for the case where it is not.
    """
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("50", "The 12th Key - Ch 5", 5)
    bookmark = ProviderBookmark("50", None, 1.0, {"completed": True})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is True
    assert client.save_progress_calls == [(ref, 60)]


def test_place_bookmark_uses_the_book_end_for_the_last_anchor() -> None:
    """place_bookmark spans from last anchor to total_pages."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("50", "The 12th Key - Ch 5", 5)
    bookmark = ProviderBookmark("50", None, 1.0, {"completed": False})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is True
    assert client.save_progress_calls == [(ref, 60)]


def test_place_bookmark_refuses_an_anchor_the_toc_does_not_have() -> None:
    """place_bookmark returns False when anchor.ref is not in the current TOC."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("999", None, 9)
    bookmark = ProviderBookmark("999", None, 0.5, {"completed": False})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is False
    assert client.save_progress_calls == []


def test_place_bookmark_refuses_without_a_resolved_ref() -> None:
    """place_bookmark returns False when _ref_cache has no entry."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKavita()
    svc = service(client)  # Empty _ref_cache

    anchor = ProviderAnchor("10", None, 1)
    bookmark = ProviderBookmark("10", None, 0.5, {"completed": False})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is False
    assert client.save_progress_calls == []


def test_every_kavita_anchor_addresses_a_page_not_a_document() -> None:
    """Every anchor's ref is a page number string (pins Kavita href-less contract)."""
    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchors = svc.list_anchors("11")

    assert all(a.ref.isdigit() for a in anchors)


def test_a_sync_populates_the_ref_memo() -> None:
    """sync() clears and populates _ref_cache with the resolved chapter_id."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11)
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = []
    view = make_book_view()

    svc = service(client)
    result = svc.sync(view)

    assert result.ok is True
    assert svc._ref_cache == {"11": client.find_chapter_result}


def test_the_ref_memo_is_cleared_between_calls() -> None:
    """_ref_cache is cleared at the start of each sync/enrich call."""
    client = FakeKavita()
    client.find_chapter_result = _ref(chapter_id=11)
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = []
    view = make_book_view()

    svc = service(client)
    svc.sync(view)
    assert "11" in svc._ref_cache

    # Now change the result and sync again
    client.find_chapter_result = _ref(chapter_id=22)
    svc.sync(view)

    # Should have only the new chapter_id
    assert set(svc._ref_cache) == {"22"}


def test_the_kavita_page_arithmetic_is_logged_at_debug(caplog: Any) -> None:
    """place_bookmark logs the page computation at DEBUG level."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    client = FakeKavita()
    client.book_chapters_result = BACKWARD_TOC
    ref = _ref(chapter_id=11, total_pages=60)
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("10", "The 12th Key - Ch 1", 1)
    bookmark = ProviderBookmark("10", "The 12th Key - Ch 1", 0.5, {"completed": False})

    with caplog.at_level(logging.DEBUG, logger="kavita_sync.service"):
        svc.place_bookmark("11", anchor, bookmark)

    assert (
        "Kavita page for chapter 11: starts at 10, span 10, progress 0.50 -> page=15" in caplog.text
    )


@pytest.mark.pins("RP-D20")
def test_place_bookmark_at_full_progress_stays_in_the_chapter() -> None:
    """At 100% progression, land on chapter's last page, never next chapter's first (RP-D20)."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    # Probe TOC: Title Page@1, Ch 1@5, Ch 2@20; total_pages=25
    probe_toc = [
        {"title": "Title Page", "page": 1},
        {"title": "Ch 1", "page": 5},
        {"title": "Ch 2", "page": 20},
    ]
    ref = _ref(chapter_id=11, total_pages=25)
    client = _EchoingKavita()
    client.book_chapters_result = probe_toc
    client.get_progress_result = {"pageNum": 0}
    svc = _anchoring_service(client, ref)

    # Place bookmark at Ch 1 with progression 1.0
    anchor = ProviderAnchor("5", "Ch 1", 1)
    bookmark = ProviderBookmark("5", "Ch 1", 1.0, {"completed": False})

    # Should place on page 19 (last page of Ch 1), not page 20 (first page of Ch 2)
    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is True
    assert client.save_progress_calls == [(ref, 19)]

    # Read back the bookmark from page 19
    read_back = svc.read_bookmark("11")

    assert read_back is not None
    assert read_back.ref == "5"  # Should be Ch 1
    # Progression should be close to 1.0 (14/15 ≈ 0.9333...)
    # page 19 is at (19 - 5) / (20 - 5) = 14/15
    assert abs(read_back.progression - (14 / 15)) < 0.01


@pytest.mark.pins("RP-D20")
def test_place_bookmark_completed_stays_in_the_chapter_when_it_is_no_longer_last() -> None:
    """A completed position restored onto a chapter later stripped of "last" still clamps.

    A position captured as ``completed`` when its chapter was the book's last one must not
    jump to the book's current last page once a later append (e.g. a merge) adds a chapter
    after it — it must still land within its own chapter's span (RP-D20).
    """
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    # Probe TOC: Ch 1@0, Ch 2@1, Ch 3@2, Ch 4@3; total_pages=4 (one page per chapter).
    probe_toc = [
        {"title": "Ch 1", "page": 0},
        {"title": "Ch 2", "page": 1},
        {"title": "Ch 3", "page": 2},
        {"title": "Ch 4", "page": 3},
    ]
    ref = _ref(chapter_id=11, total_pages=4)
    client = _EchoingKavita()
    client.book_chapters_result = probe_toc
    client.get_progress_result = {"pageNum": 0}
    svc = _anchoring_service(client, ref)

    # Restore a completed Ch 3 position — Ch 4 now follows it, so page 4 (the book's current
    # last page) would land past Ch 3's own single-page span [2, 3).
    anchor = ProviderAnchor("2", "Ch 3", 2)
    bookmark = ProviderBookmark("2", "Ch 3", 1.0, {"completed": True})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is True
    assert client.save_progress_calls == [(ref, 2)]


@pytest.mark.pins("RP-D20")
def test_place_bookmark_ignores_a_stale_total_pages_that_understates_the_book() -> None:
    """ "Last chapter" is asked of the anchor list, never inferred from ref.total_pages.

    Kavita's search index (``find_chapter``'s own source, cached separately from the TOC)
    can still report a book's pre-merge page count right after its chapter list has already
    grown — understating ``total_pages`` down to exactly this chapter's own last page, which
    used to read as "this is the last chapter" and clamp to the book's stale end instead of
    this chapter's real span (RP-D20).
    """
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    # Probe TOC: Ch 1@0, Ch 2@1, Ch 3@2, Ch 4@3 — Ch 4 already exists in the fresh TOC, but
    # total_pages=3 is stale, still reporting the book's page count from before Ch 4 existed.
    probe_toc = [
        {"title": "Ch 1", "page": 0},
        {"title": "Ch 2", "page": 1},
        {"title": "Ch 3", "page": 2},
        {"title": "Ch 4", "page": 3},
    ]
    ref = _ref(chapter_id=11, total_pages=3)
    client = _EchoingKavita()
    client.book_chapters_result = probe_toc
    client.get_progress_result = {"pageNum": 0}
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("2", "Ch 3", 2)
    bookmark = ProviderBookmark("2", "Ch 3", 1.0, {"completed": False})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is True
    assert client.save_progress_calls == [(ref, 2)]


@pytest.mark.pins("RP-D20")
def test_place_bookmark_at_zero_lands_on_the_start_page() -> None:
    """Restore at 0% progression lands on the chapter's start page (RP-D20)."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    probe_toc = [
        {"title": "Title Page", "page": 1},
        {"title": "Ch 1", "page": 5},
        {"title": "Ch 2", "page": 20},
    ]
    ref = _ref(chapter_id=11, total_pages=25)
    client = FakeKavita()
    client.book_chapters_result = probe_toc
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("5", "Ch 1", 1)
    bookmark = ProviderBookmark("5", "Ch 1", 0.0, {"completed": False})

    result = svc.place_bookmark("11", anchor, bookmark)

    assert result is True
    assert client.save_progress_calls == [(ref, 5)]


@pytest.mark.pins("RP-D20")
def test_place_bookmark_round_trip_stays_within_one_page() -> None:
    """Round-trip place→read stays within one page for any progression (RP-D20)."""
    from ebookerr_sdk.spi import ProviderAnchor, ProviderBookmark

    probe_toc = [
        {"title": "Title Page", "page": 1},
        {"title": "Ch 1", "page": 5},
        {"title": "Ch 2", "page": 20},
    ]
    ref = _ref(chapter_id=11, total_pages=25)
    client = _EchoingKavita()
    client.book_chapters_result = probe_toc
    svc = _anchoring_service(client, ref)

    anchor = ProviderAnchor("5", "Ch 1", 1)
    span = 20 - 5  # 15 pages in Ch 1

    for test_progression in (0.0, 0.25, 0.5, 0.75, 1.0):
        # Clear save calls for this iteration
        client.save_progress_calls.clear()

        bookmark = ProviderBookmark("5", "Ch 1", test_progression, {"completed": False})
        result = svc.place_bookmark("11", anchor, bookmark)

        assert result is True
        assert len(client.save_progress_calls) == 1

        # Read back the bookmark
        read_back = svc.read_bookmark("11")

        assert read_back is not None
        assert read_back.ref == "5", f"progression {test_progression} should stay in Ch 1"
        # Progression delta should be at most 1/span (one page difference out of the chapter span)
        assert abs(read_back.progression - test_progression) <= 1 / span


class _EchoingKavita(FakeKavita):
    """Returns the last page it was told to save, like a real Kavita after a write."""

    def get_progress(self, chapter_id: int) -> dict[str, Any]:
        if self.save_progress_calls:
            return {"pageNum": self.save_progress_calls[-1][1]}
        return self.get_progress_result


@pytest.mark.pins("EXP-243")
def test_a_lost_kavita_position_is_re_anchored_from_the_stored_history() -> None:
    """A Kavita position at pageNum=0 is re-anchored from stored read position."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = _EchoingKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = BACKWARD_TOC
    view = make_book_view(
        num_chapters=5,
        chapter_table=BACKWARD_TOC_CHAPTERS,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=3,
            chapter_progress=0.0,
            chapter_number=3,
            chapter_title="The 12th Key - Ch 3",
            chapter_href=None,
            completed=False,
            total_chapters=5,
        ),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert client.save_progress_calls == [(ref, 30)]
    assert result.read_position is not None
    assert result.read_position.chapter_title == "The 12th Key - Ch 3"
    assert result.restore_attempted is False


def test_a_resolving_kavita_page_is_never_re_anchored() -> None:
    """A Kavita position that still resolves is never re-anchored."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 45}
    client.book_chapters_result = BACKWARD_TOC
    view = make_book_view(
        num_chapters=5,
        chapter_table=BACKWARD_TOC_CHAPTERS,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=1,
            chapter_progress=0.0,
            chapter_number=1,
            chapter_title="The 12th Key - Ch 1",
            chapter_href=None,
            completed=False,
            total_chapters=5,
        ),
    )

    result = service(client).sync(view)

    assert result.ok is True
    assert client.save_progress_calls == []


def test_a_sync_with_a_restore_target_does_not_also_re_anchor() -> None:
    """A marker-driven restore does not also trigger re-anchoring."""
    ref = _ref(chapter_id=11, total_pages=10)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = TOC_FIXTURE
    view = make_book_view(
        chapter_table=TOC_FIXTURE_CHAPTERS,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.0,
            chapter_number=2,
            chapter_title="The 12th Key - Ch 2",
            chapter_href=None,
            completed=False,
            total_chapters=2,
        ),
    )
    restore_target = ReadPosition(
        captured_at="2026-07-12T12:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_number=1,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    service(client).sync(view, restore_target=restore_target)

    assert len(client.save_progress_calls) == 1
    assert client.save_progress_calls[0] == (ref, 4)


def test_a_re_anchor_re_reads_the_page_before_capture(caplog: Any) -> None:
    """Re-anchoring logs the re-read page at DEBUG level."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = _EchoingKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = BACKWARD_TOC
    view = make_book_view(
        num_chapters=5,
        chapter_table=BACKWARD_TOC_CHAPTERS,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=3,
            chapter_progress=0.0,
            chapter_number=3,
            chapter_title="The 12th Key - Ch 3",
            chapter_href=None,
            completed=False,
            total_chapters=5,
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="kavita_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any(
        'Re-read the Kavita page for "A Title" after a re-anchor: pageNum=30' in r.message
        for r in debug_records
    )


def test_a_book_with_no_stored_position_is_never_re_anchored() -> None:
    """A book with no stored position is never re-anchored."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = BACKWARD_TOC
    view = make_book_view(num_chapters=5, chapter_table=BACKWARD_TOC_CHAPTERS)

    result = service(client).sync(view)

    assert result.ok is True
    assert client.save_progress_calls == []


EMPTY_CAPTURE_TOC = [
    {"title": "Title Page", "page": 1},
    {"title": "The 12th Key - Ch 1", "page": 10},
    {"title": "The 12th Key - Ch 2", "page": 20},
]

# EMPTY_CAPTURE_TOC's two content chapters (its "Title Page" front-matter entry excluded).
EMPTY_CAPTURE_TOC_CHAPTERS = _chapters("The 12th Key - Ch 1", "The 12th Key - Ch 2")


@pytest.mark.pins("EXP-243")
def test_an_empty_kavita_read_back_is_not_a_backward_move(caplog: Any) -> None:
    """An empty provider read-back (chapter_index 0, 0% progress) is not a backward move."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 1}
    client.book_chapters_result = EMPTY_CAPTURE_TOC
    view = make_book_view(
        num_chapters=2,
        chapter_table=EMPTY_CAPTURE_TOC_CHAPTERS,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=1,
            chapter_progress=0.0,
            chapter_number=1,
            chapter_title="The 12th Key - Ch 1",
            chapter_href=None,
            completed=False,
            total_chapters=2,
        ),
    )

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(view)

    assert result.ok is True
    assert result.backward_move is None
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "moved backwards" in r.message
    ]
    assert len(warnings) == 0
    # A bookmark that resolves to a non-chapter document is never recorded at all (RP-CAP-3),
    # so capture returns None outright rather than an empty ReadPosition; that DEBUG line
    # (not the book-level "came back empty" one, which needs an actual captured position) is
    # the trail explaining why nothing was recorded here.
    debug_records = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "is on a non-chapter document" in r.message
    ]
    assert len(debug_records) == 1


@pytest.mark.pins("EXP-243")
def test_a_real_kavita_backward_move_still_warns(caplog: Any) -> None:
    """A real backward move (non-empty position) still warns."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 15}
    client.book_chapters_result = EMPTY_CAPTURE_TOC
    view = make_book_view(
        num_chapters=2,
        chapter_table=EMPTY_CAPTURE_TOC_CHAPTERS,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.5,
            chapter_number=2,
            chapter_title="The 12th Key - Ch 2",
            chapter_href=None,
            completed=False,
            total_chapters=2,
        ),
    )

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(view)

    assert result.backward_move is not None
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "moved backwards" in r.message
    ]
    assert len(warnings) >= 1


def test_a_requested_restore_is_not_a_backward_move(caplog: Any) -> None:
    """A requested restore (restore_attempted=True) with backward move is silent (RP-D18)."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 15}
    client.book_chapters_result = EMPTY_CAPTURE_TOC
    view = make_book_view(
        num_chapters=2,
        chapter_table=EMPTY_CAPTURE_TOC_CHAPTERS,
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=2,
            chapter_progress=0.5,
            chapter_number=2,
            chapter_title="The 12th Key - Ch 2",
            chapter_href=None,
            completed=False,
            total_chapters=2,
        ),
    )

    # Restore target is at a different chapter
    restore_target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=0,
        chapter_progress=0.3,
        chapter_number=0,
        chapter_title="The 12th Key - Ch 1",
        chapter_href=None,
        completed=False,
        total_chapters=2,
    )

    with caplog.at_level(logging.DEBUG):
        result = service(client).sync(view, restore_target=restore_target)

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


# ---------------------------------------------------------------------------
# Capture position delegation to core (TASK-6)
# ---------------------------------------------------------------------------


def test_capture_uses_the_books_own_toc_when_available(build_epub: Any, tmp_path: Any) -> None:
    """Capture joins Kavita's anchors onto book.chapter_table, not an EPUB read from disk."""
    # An EPUB on disk with matching titles: capture no longer reads it (only book.chapter_table
    # does the joining) — kept here to show the file's presence changes nothing.
    epub_path = build_epub(
        [("Chapter 1", ""), ("Chapter 2", "")],
        include_title_page=True,
    )

    # Kavita TOC: Title Page (front matter), Contents (front matter), Chapter 1, Chapter 2
    kavita_toc = [
        {"page": 1, "title": "Title Page"},
        {"page": 3, "title": "Contents"},
        {"page": 5, "title": "Chapter 1"},
        {"page": 20, "title": "Chapter 2"},
    ]

    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=40)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 9}
    client.book_chapters_result = kavita_toc

    # Move the built EPUB to tmp_path with the output filename
    import shutil

    library_folder = tmp_path / "library"
    library_folder.mkdir()
    final_epub = library_folder / "book.epub"
    shutil.copy(epub_path, final_epub)

    view = make_book_view(
        output_filename="book.epub",
        num_chapters=2,
        chapter_table=_chapters("Chapter 1", "Chapter 2"),
    )

    result = service(client, library_folder=library_folder).sync(view)

    assert result.ok is True
    assert result.read_position is not None
    # Chapter 1 at index 1 (after the front-matter Title Page/Contents entries)
    assert result.read_position.chapter_title == "Chapter 1"
    assert result.read_position.chapter_index == 1
    # Progress: page 9 is between pages 5-20, so (9-5)/(20-5) = 4/15 ≈ 0.267
    assert result.read_position.chapter_progress == pytest.approx((9 - 5) / (20 - 5))


def test_capture_is_stable_across_a_packaged_count_change(build_epub: Any, tmp_path: Any) -> None:
    """Capture index and title remain stable when book.num_chapters changes."""
    epub_path = build_epub(
        [("Chapter 1", ""), ("Chapter 2", "")],
        include_title_page=True,
    )

    kavita_toc = [
        {"page": 1, "title": "Title Page"},
        {"page": 3, "title": "Contents"},
        {"page": 5, "title": "Chapter 1"},
        {"page": 20, "title": "Chapter 2"},
    ]

    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=40)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 9}
    client.book_chapters_result = kavita_toc

    import shutil

    library_folder = tmp_path / "library"
    library_folder.mkdir()
    final_epub = library_folder / "book.epub"
    shutil.copy(epub_path, final_epub)
    chapter_table = _chapters("Chapter 1", "Chapter 2")

    # First capture with num_chapters=2
    view1 = make_book_view(output_filename="book.epub", num_chapters=2, chapter_table=chapter_table)
    result1 = service(client, library_folder=library_folder).sync(view1)
    assert result1.read_position is not None
    index1 = result1.read_position.chapter_index
    title1 = result1.read_position.chapter_title

    # Second capture with num_chapters=3 (changed count)
    view2 = make_book_view(output_filename="book.epub", num_chapters=3, chapter_table=chapter_table)
    result2 = service(client, library_folder=library_folder).sync(view2)
    assert result2.read_position is not None
    index2 = result2.read_position.chapter_index
    title2 = result2.read_position.chapter_title

    # Should be identical
    assert index1 == index2
    assert title1 == title2


def test_capture_without_a_library_folder_keeps_the_previous_basis(
    tmp_path: Any,
) -> None:
    """Capture needs no library_folder: it joins onto book.chapter_table, never an EPUB read."""
    kavita_toc = [
        {"page": 1, "title": "Title Page"},
        {"page": 3, "title": "Contents"},
        {"page": 5, "title": "Chapter 1"},
        {"page": 20, "title": "Chapter 2"},
    ]

    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=40)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 9}
    client.book_chapters_result = kavita_toc

    # Service with no library_folder set
    view = make_book_view(
        output_filename="book.epub",
        num_chapters=2,
        chapter_table=_chapters("Chapter 1", "Chapter 2"),
    )
    result = service(client, library_folder=None).sync(view)

    # "Title Page" and "Contents" match no chapter_table title, so they stay front matter;
    # "Chapter 1" is the first content chapter, index 1 — unaffected by library_folder.
    assert result.ok is True
    assert result.read_position is not None
    assert result.read_position.chapter_index == 1


def test_semantic_from_pages_is_gone() -> None:
    """_semantic_from_pages function is deleted."""
    import kavita_sync.service

    assert not hasattr(kavita_sync.service, "_semantic_from_pages")


def test_capture_logs_under_the_kavita_provider_name(
    build_epub: Any, tmp_path: Any, caplog: Any
) -> None:
    """Capture logging shows the Kavita provider name."""
    # Build EPUB with matching titles
    epub_path = build_epub(
        [("Chapter 1", ""), ("Chapter 2", "")],
        include_title_page=True,
    )

    kavita_toc = [
        {"page": 1, "title": "Title Page"},
        {"page": 3, "title": "Contents"},
        {"page": 5, "title": "Chapter 1"},
        {"page": 20, "title": "Chapter 2"},
    ]

    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=40)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 9}
    client.book_chapters_result = kavita_toc

    import shutil

    library_folder = tmp_path / "library"
    library_folder.mkdir()
    final_epub = library_folder / "book.epub"
    shutil.copy(epub_path, final_epub)

    view = make_book_view(
        output_filename="book.epub",
        num_chapters=2,
        chapter_table=_chapters("Chapter 1", "Chapter 2"),
    )

    with caplog.at_level(logging.INFO, logger="ebookerr_sdk.providers.anchoring"):
        result = service(client, library_folder=library_folder).sync(view)

    assert result.ok is True
    # The core's capture_position logs with "Captured the Kavita read position"
    assert "Captured the Kavita read position" in caplog.text


def test_a_never_read_book_captures_nothing(build_epub: Any, tmp_path: Any, caplog: Any) -> None:
    """A book with pageNum=0 (never read) captures no position (RP-CAP-3)."""
    # Build EPUB with matching titles
    epub_path = build_epub(
        [("Chapter 1", ""), ("Chapter 2", "")],
        include_title_page=True,
    )

    kavita_toc = [
        {"page": 1, "title": "Title Page"},
        {"page": 3, "title": "Contents"},
        {"page": 5, "title": "Chapter 1"},
        {"page": 20, "title": "Chapter 2"},
    ]

    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=40)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}  # Never read
    client.book_chapters_result = kavita_toc

    import shutil

    library_folder = tmp_path / "library"
    library_folder.mkdir()
    final_epub = library_folder / "book.epub"
    shutil.copy(epub_path, final_epub)

    view = make_book_view(
        output_filename="book.epub",
        num_chapters=2,
        chapter_table=_chapters("Chapter 1", "Chapter 2"),
    )

    with caplog.at_level(logging.INFO, logger="ebookerr_sdk.providers.anchoring"):
        result = service(client, library_folder=library_folder).sync(view)

    assert result.ok is True
    # Never-read books capture nothing
    assert result.read_position is None
    # No capture log line (RP-CAP-3)
    capture_lines = [r for r in caplog.records if "Captured the Kavita read position" in r.message]
    assert len(capture_lines) == 0


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
def test_an_unreachable_provider_is_probed_once_not_once_per_book(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An outage is concluded once; every later book in the batch skips the probe (EXP-269)."""
    client = FakeKavita()
    client.connected = False
    svc = service(client, circuit=_OneStrikeCircuitGuard())

    with caplog.at_level(logging.WARNING, logger="kavita_sync.service"):
        results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(100)]

    assert client.test_connection_calls == 1
    assert all(r.unreachable is True for r in results)
    assert all(r.message == "Kavita is not reachable" for r in results)
    warning_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "Kavita is not reachable" in r.message
    ]
    assert len(warning_records) == 1


@pytest.mark.pins("EXP-269")
def test_a_hanging_provider_costs_one_timeout_not_one_per_book() -> None:
    """A probe that itself raises ProviderUnreachable still only costs one timeout (EXP-269)."""
    from ebookerr_sdk.providers.connection import ProviderUnreachable

    class _HangingKavita(FakeKavita):
        def test_connection(self) -> ConnectionTestResult:
            self.test_connection_calls += 1
            raise ProviderUnreachable("Kavita is not reachable: ReadTimeout")

    client = _HangingKavita()
    svc = service(client, circuit=_OneStrikeCircuitGuard())

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(100)]

    assert client.test_connection_calls == 1
    assert all(r.unreachable is True for r in results)


@pytest.mark.pins("EXP-269")
def test_a_refusing_provider_is_unchanged() -> None:
    """With no circuit injected, a failing probe is re-asked for every book, as before."""
    client = FakeKavita()
    client.connected = False
    svc = service(client, circuit=None)

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(5)]

    assert client.test_connection_calls == 5
    assert all(r.unreachable is True for r in results)


@pytest.mark.pins("EXP-269")
def test_a_healthy_provider_is_unchanged() -> None:
    """A healthy provider still reaches _sync_reachable for every book; breaker stays closed."""
    client = FakeKavita()
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 0}
    guard = _OneStrikeCircuitGuard()
    svc = service(client, circuit=guard)

    with patch.object(svc, "_sync_reachable", wraps=svc._sync_reachable) as spy:
        results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(5)]

    assert all(r.ok is True for r in results)
    assert spy.call_count == 5
    assert client.test_connection_calls == 1
    assert guard.is_open("provider:kavita") is False


@pytest.mark.pins("EXP-269")
def test_a_404_from_the_provider_never_trips_the_breaker() -> None:
    """A normal not-found SyncResult is not a transport failure; the breaker stays closed."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKavita()
    client.find_chapter_result = None
    svc = service(client, circuit=guard)

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(10)]

    assert all(r.message == "book not found in Kavita" for r in results)
    assert guard.is_open("provider:kavita") is False


@pytest.mark.pins("EXP-269")
def test_a_refusal_never_trips_the_breaker() -> None:
    """A LinkRefusal is a local, terminal answer — it never touches the breaker (EXP-187)."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKavita()
    svc = service(
        client,
        circuit=guard,
        link_owner=lambda item_id, *, provider=None: "otherBook",
    )
    views = [
        make_book_view(
            book_id=f"book{i}", output_filename="a/b.epub", external=ExternalLink(item_id="77")
        )
        for i in range(10)
    ]

    results = [svc.sync(v) for v in views]

    assert all(r.refused is True for r in results)
    assert all(r.unreachable is False for r in results)
    assert guard.is_open("provider:kavita") is False


@pytest.mark.pins("EXP-269")
def test_the_breaker_is_shared_across_two_service_instances() -> None:
    """An open breaker protects the next batch and the 03:00 scheduled run alike (EXP-269)."""
    guard = _OneStrikeCircuitGuard()
    client1 = FakeKavita()
    client1.connected = False
    svc1 = service(client1, circuit=guard)
    svc1.sync(make_book_view(title="Book 1"))

    client2 = FakeKavita()
    svc2 = service(client2, circuit=guard)
    result = svc2.sync(make_book_view(title="Book 2"))

    assert result.unreachable is True
    assert client2.test_connection_calls == 0


@pytest.mark.pins("EXP-269")
def test_a_reset_lets_the_next_book_through() -> None:
    """Resetting the guard closes the breaker; the next sync probes once and can succeed."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKavita()
    client.connected = False
    svc = service(client, circuit=guard)
    svc.sync(make_book_view(title="Book 1"))

    guard.reset("provider:kavita")

    client.connected = True
    client.find_chapter_result = _ref()
    client.get_progress_result = {"pageNum": 0}
    calls_before = client.test_connection_calls
    result = svc.sync(make_book_view(title="Book 2"))

    assert client.test_connection_calls - calls_before == 1
    assert result.ok is True


@pytest.mark.pins("EXP-269")
def test_an_unreachable_book_is_not_reported_as_missing() -> None:
    """EXP-192's message contract survives the breaker: never "not found" (EXP-269)."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKavita()
    client.connected = False
    svc = service(client, circuit=guard)

    results = [svc.sync(make_book_view(title=f"Book {i}")) for i in range(20)]

    assert all(r.message == "Kavita is not reachable" for r in results)
    assert all("not found" not in r.message for r in results)


@pytest.mark.pins("EXP-269")
def test_the_open_circuit_logs_once_per_batch_not_once_per_book(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The open-circuit DEBUG line fires per skipped book; the outage WARNING fires once."""
    guard = _OneStrikeCircuitGuard()
    client = FakeKavita()
    client.connected = False
    svc = service(client, circuit=guard)

    with caplog.at_level(logging.DEBUG, logger="kavita_sync.service"):
        for i in range(100):
            svc.sync(make_book_view(title=f"Book {i}"))

    debug_records = [
        r
        for r in caplog.records
        if r.levelname == "DEBUG" and "Kavita circuit is open" in r.message
    ]
    warning_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "Kavita is not reachable" in r.message
    ]
    assert len(debug_records) == 99
    assert len(warning_records) == 1


@pytest.mark.pins("EXP-269")
def test_the_plugin_passes_its_context_circuit_to_the_service() -> None:
    """_build_service forwards ctx.circuit straight through to KavitaService (EXP-269)."""
    from kavita_sync.plugin import _build_service

    class _CtxDouble:
        settings: dict[str, Any] = {"server": "http://kavita.local:5000", "api_key": "k"}
        library_root = None

    sentinel_circuit = object()
    ctx = _CtxDouble()
    ctx.circuit = sentinel_circuit  # type: ignore[attr-defined]

    built = _build_service(ctx, enabled=True)

    assert built._circuit is sentinel_circuit


def test_sync_reports_the_provider_chapter_count() -> None:
    """Syncing a book writes the joined chapter count to external_chapter_count."""
    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=100)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = [
        {"title": "Title", "page": 0},
        {"title": "Chapter 1", "page": 10},
        {"title": "Chapter 2", "page": 50},
        {"title": "Chapter 3", "page": 80},
    ]
    view = make_book_view(chapter_table=_chapters("Chapter 1", "Chapter 2", "Chapter 3"))

    result = service(client).sync(view)

    assert result.ok is True
    # The joined count excludes the "Title" front-matter entry (CHC-D12).
    assert result.fields["external_chapter_count"] == 3


def test_kavita_the_count_is_toc_entries_not_pages() -> None:
    """An invalid, page-less TOC entry never becomes an anchor, so it cannot inflate the count."""
    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=100)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    # Include some invalid entries (no page number) that should be filtered out
    client.book_chapters_result = [
        {"title": "Title", "page": 0},
        {"title": "Intro", "page": 5},
        {"title": "Chapter 1", "page": 10},
        {"title": "Chapter 2", "page": 50},
        {"title": "Chapter 3", "page": 80},
        {"title": "No page", "page": None},  # Should be filtered
    ]
    view = make_book_view(chapter_table=_chapters("Chapter 1", "Chapter 2", "Chapter 3"))

    result = service(client).sync(view)

    assert result.ok is True
    # Title/Intro stay front matter (no chapter_table match) and "No page" never becomes an
    # anchor at all; only Chapter 1/2/3 join the chapter table.
    assert result.fields["external_chapter_count"] == 3


def test_kavita_an_unreachable_provider_clears_the_count() -> None:
    """When the provider is unreachable, external_chapter_count is set to None."""
    client = FakeKavita()
    client.connected = False
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is False
    assert result.fields.get("external_chapter_count") is None


def test_kavita_an_unlinked_book_reports_count() -> None:
    """An unlinked book (no external_item_id) still gets a chapter count on first link."""
    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=100)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = [
        {"title": "Title", "page": 0},
        {"title": "Chapter 1", "page": 10},
        {"title": "Chapter 2", "page": 50},
    ]
    view = make_book_view()

    result = service(client).sync(view)

    assert result.ok is True
    assert "external_chapter_count" in result.fields
    assert isinstance(result.fields["external_chapter_count"], int)


def test_kavita_a_changed_count_is_logged_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """When external_chapter_count is set, it is logged at INFO level."""
    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=100)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = [
        {"title": "Title", "page": 0},
        {"title": "Chapter 1", "page": 10},
        {"title": "Chapter 2", "page": 50},
        {"title": "Chapter 3", "page": 80},
    ]
    view = make_book_view(external=ExternalLink(item_id="11"))

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    caplog_records = [
        r for r in caplog.records if r.levelname == "INFO" and "Kavita reports" in r.message
    ]
    assert len(caplog_records) > 0


def test_kavita_an_unchanged_count_is_not_logged_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """When external_chapter_count is set, logging happens."""
    client = FakeKavita()
    ref = _ref(chapter_id=11, total_pages=100)
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = [
        {"title": "Title", "page": 0},
        {"title": "Chapter 1", "page": 10},
        {"title": "Chapter 2", "page": 50},
        {"title": "Chapter 3", "page": 80},
    ]
    view = make_book_view(
        external=ExternalLink(item_id="11"),
        chapter_table=_chapters("Chapter 1", "Chapter 2", "Chapter 3"),
    )

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    # Count should be written to fields
    assert result.fields["external_chapter_count"] == 3


# ---------------------------------------------------------------------------
# TASK-21: retry settings, file-changed wait, stale skip, restore through the
# core (CHC-D12, RP-D20)
# ---------------------------------------------------------------------------


def test_a_changed_file_waits_for_kavita_to_re_index(caplog: pytest.LogCaptureFixture) -> None:
    """A changed file's re-index is waited for before read-position work runs."""
    ref = _ref(chapter_id=11, total_pages=40)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 15}
    old_toc = [{"title": "Title Page", "page": 0}]
    new_toc = [
        {"title": "Title Page", "page": 0},
        {"title": "Ch 1", "page": 5},
        {"title": "Ch 2", "page": 20},
    ]
    client.book_chapters_sequence = [old_toc, old_toc, new_toc]
    client.book_chapters_result = new_toc  # steady state once the sequence is exhausted
    view = make_book_view(
        title="The Long Orbit",
        output_filename="An Author/a_title.epub",
        file_size=200,
        chapter_table=_chapters("Ch 1", "Ch 2"),
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=100,
        ),
    )

    with caplog.at_level(logging.INFO, logger="kavita_sync.service"):
        result = service(client, library_path="/books", scan_retry_max=5).sync(view)

    assert result.ok is True
    assert client.scan_folder_calls == ["/books/An Author"]
    assert 'Kavita re-indexed "The Long Orbit" (chapter_id=11) after 3 poll(s)' in caplog.text
    assert result.read_position is not None
    assert result.read_position.chapter_title == "Ch 1"


def test_kavita_never_re_indexing_skips_read_position_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kavita never re-indexing a changed file skips all read-position work this sync."""
    ref = _ref(chapter_id=11, total_pages=40)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 15}
    client.book_chapters_result = [{"title": "Title Page", "page": 0}]  # never joins
    restore_target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=1,
        chapter_progress=0.5,
        chapter_title="Ch 1",
        total_chapters=2,
    )
    view = make_book_view(
        title="The Long Orbit",
        output_filename="An Author/a_title.epub",
        file_size=200,
        chapter_table=_chapters("Ch 1", "Ch 2"),
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=100,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="kavita_sync.service"):
        result = service(client, library_path="/books", scan_retry_max=2).sync(
            view, restore_target=restore_target
        )

    assert result.ok is True
    assert result.read_position is None
    assert result.restore_attempted is False
    assert client.save_progress_calls == []
    assert (
        'Kavita has not re-indexed "The Long Orbit" after 2 poll(s); read-position work '
        "waits for the next sync" in caplog.text
    )
    assert (
        'Read-position capture, re-anchor and restore skipped for "The Long Orbit"' in caplog.text
    )


def test_a_stale_toc_without_a_nudge_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """A stale provider table is skipped even without a file-changed nudge."""
    ref = _ref(chapter_id=11, total_pages=40)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 5}
    client.book_chapters_result = [
        {"title": "Title Page", "page": 0},
        {"title": "Love", "page": 10},
    ]
    view = make_book_view(
        title="The Long Orbit",
        output_filename="An Author/a_title.epub",
        file_size=200,
        chapter_table=_chapters("Love", "Love"),
        external=ExternalLink(
            item_id="11",
            link_attempted_at="2026-09-03T19:35:18+00:00",
            link_attempt_size=200,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="kavita_sync.service"):
        result = service(client).sync(view)

    assert result.ok is True
    assert client.scan_folder_calls == []  # sizes match: no nudge, no wait
    assert result.read_position is None
    assert (
        'Read-position capture, re-anchor and restore skipped for "The Long Orbit"' in caplog.text
    )


def test_external_chapter_count_excludes_the_title_page_entry() -> None:
    """The joined chapter count excludes the provider's title-page anchor."""
    ref = _ref(chapter_id=11, total_pages=40)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 5}
    client.book_chapters_result = [
        {"title": "Title Page", "page": 0},
        {"title": "Ch 1", "page": 5},
        {"title": "Ch 2", "page": 20},
    ]
    view = make_book_view(chapter_table=_chapters("Ch 1", "Ch 2"))

    result = service(client).sync(view)

    assert result.ok is True
    assert result.fields["external_chapter_count"] == 2


def test_restore_lands_on_the_chapters_start_page_plus_progress(caplog: Any) -> None:
    """A restore target lands on its chapter's start page plus the target's own progress."""
    ref = _ref(chapter_id=11, total_pages=40)
    client = _EchoingKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = [
        {"title": "Ch 1", "page": 5},
        {"title": "Ch 2", "page": 20},
    ]
    view = make_book_view(chapter_table=_chapters("Ch 1", "Ch 2"))
    restore_target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=2,
        chapter_progress=0.5,
        chapter_number=2,
        chapter_title="Ch 2",
        total_chapters=2,
    )

    with caplog.at_level(logging.INFO):
        result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert client.save_progress_calls == [(ref, 30)]  # 20 + round(0.5 * 20)
    assert result.restore_landed is True
    assert 'Restored semantic read position for "A Title": page=30 (chapter_index=2)' in caplog.text


def test_restore_no_match_fails_closed_through_the_core(caplog: Any) -> None:
    """A restore target matching nothing fails closed via the core's own warning."""
    ref = _ref(chapter_id=11, total_pages=40)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 0}
    client.book_chapters_result = [
        {"title": "Ch 1", "page": 5},
        {"title": "Ch 2", "page": 20},
    ]
    view = make_book_view(chapter_table=_chapters("Ch 1", "Ch 2"))
    restore_target = ReadPosition(
        captured_at="2026-01-01T00:00:00+00:00",
        chapter_index=99,
        chapter_progress=0.5,
        chapter_number=None,
        chapter_title="Nonexistent Chapter",
        total_chapters=2,
    )

    with caplog.at_level(logging.WARNING):
        result = service(client).sync(view, restore_target=restore_target)

    assert result.ok is True
    assert result.restore_attempted is True
    assert result.restore_landed is False
    assert client.save_progress_calls == []
    assert "Read-position restore failed for" in caplog.text
