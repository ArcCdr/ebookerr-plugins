"""The backward-move notice on Komga states the terms that show the move (EXP-207)."""

import logging

import pytest
from ebookerr_sdk.spi import ExternalLink, ReadPosition
from ebookerr_sdk.testing import make_book_view
from test_komga_service import (
    _DEFAULT_CHAPTER_TABLE,
    MATCHING_BOOK_METADATA,
    MATCHING_SERIES_METADATA,
    POSITIONS_FIXTURE,
    FakeKomga,
    komga_book,
)
from test_komga_service import (
    service as komga_service,
)


@pytest.mark.pins("EXP-207")
def test_the_user_message_states_the_progress_terms_on_komga(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Komga backward-move message shows chapter indices and percentages."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    progression = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 5, "progression": 0.111},
        },
        "device": {"id": "komic-ios", "name": "Komic"},
        "modified": "2026-08-08T10:00:00Z",
    }
    client.progression_sequence = [progression] * 3
    client.positions = POSITIONS_FIXTURE

    view = make_book_view(
        title="The 12th Key",
        external=ExternalLink(item_id="KB1", provider="komga"),
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

    with caplog.at_level(logging.WARNING):
        result = komga_service(client).sync(view)

    assert (
        result.backward_move
        == 'Read position for "The 12th Key" moved backwards: chapter 2 → 2, 14% → 11%'
    )
    assert any("moved backwards" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_an_unrequested_backward_move_still_warns_on_komga(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unrequested backward move (restore_attempted=False) still warns on Komga."""
    client = FakeKomga()
    client.find_results = ["KB1"]
    kb = komga_book(metadata=MATCHING_BOOK_METADATA)
    client.book_sequence = [kb, kb]
    client.series = {"metadata": MATCHING_SERIES_METADATA}
    progression = {
        "locator": {
            "href": "OEBPS/file0002.xhtml",
            "locations": {"position": 5, "progression": 0.111},
        },
        "device": {"id": "komic-ios", "name": "Komic"},
        "modified": "2026-08-08T10:00:00Z",
    }
    client.progression_sequence = [progression] * 3
    client.positions = POSITIONS_FIXTURE

    view = make_book_view(
        title="The 12th Key",
        external=ExternalLink(item_id="KB1", provider="komga"),
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

    with caplog.at_level(logging.WARNING):
        result = komga_service(client).sync(view)

    assert result.backward_move is not None
    assert any("moved backwards" in r.message for r in caplog.records if r.levelname == "WARNING")
