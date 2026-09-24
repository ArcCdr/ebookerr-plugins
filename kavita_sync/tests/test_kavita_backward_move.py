"""The backward-move notice on Kavita states the terms that show the move (EXP-207)."""

import logging

import pytest
from ebookerr_sdk.spi import ReadPosition
from ebookerr_sdk.testing import make_book_view

from tests.unit.test_kavita_service import BACKWARD_TOC, BACKWARD_TOC_CHAPTERS, FakeKavita, _ref
from tests.unit.test_kavita_service import service as kavita_service


@pytest.mark.pins("EXP-207")
def test_the_user_message_states_the_progress_terms_on_kavita(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Kavita backward-move message shows chapter indices and percentages."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 45}
    client.book_chapters_result = BACKWARD_TOC

    view = make_book_view(
        title="Salt and Circuitry (3/6)",
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=30,
            chapter_progress=0.5,
            chapter_number=30,
            chapter_title="Chapter 30",
            chapter_href=None,
            completed=False,
            total_chapters=100,
        ),
        chapter_table=BACKWARD_TOC_CHAPTERS,
    )

    with caplog.at_level(logging.WARNING):
        result = kavita_service(client).sync(view)

    assert result.backward_move is not None
    assert result.backward_move.startswith(
        'Read position for "Salt and Circuitry (3/6)" moved backwards: chapter 30 → 4, 50% → '
    )
    assert result.backward_move.endswith("%")
    assert any("moved backwards" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_an_unrequested_backward_move_still_warns_on_kavita(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unrequested backward move (restore_attempted=False) still warns on Kavita."""
    ref = _ref(chapter_id=11, total_pages=60)
    client = FakeKavita()
    client.find_chapter_result = ref
    client.get_progress_result = {"pageNum": 45}
    client.book_chapters_result = BACKWARD_TOC

    view = make_book_view(
        title="Salt and Circuitry (3/6)",
        read_position=ReadPosition(
            captured_at="2026-01-01T00:00:00+00:00",
            chapter_index=30,
            chapter_progress=0.5,
            chapter_number=30,
            chapter_title="Chapter 30",
            chapter_href=None,
            completed=False,
            total_chapters=100,
        ),
        chapter_table=BACKWARD_TOC_CHAPTERS,
    )

    with caplog.at_level(logging.WARNING):
        result = kavita_service(client).sync(view)

    assert result.backward_move is not None
    assert any("moved backwards" in r.message for r in caplog.records if r.levelname == "WARNING")
