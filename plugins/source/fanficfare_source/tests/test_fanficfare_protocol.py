"""One FanFicFare run's result (LIB-D25, LIB-D29)."""

from __future__ import annotations

import dataclasses

import pytest

from fanficfare_source.protocol import (
    DOWNLOAD_OUTCOMES,
    UNREADABLE_MESSAGE,
    UNRECOGNISED_MESSAGE,
    DownloadResult,
)


def test_every_outcome_is_listed() -> None:
    """DOWNLOAD_OUTCOMES lists all possible outcomes."""
    assert DOWNLOAD_OUTCOMES == ("created", "updated", "unrecognised", "failed")


def test_a_written_result_is_ok() -> None:
    """Results with created or updated outcome have ok=True."""
    assert DownloadResult("created").ok is True
    assert DownloadResult("updated").ok is True


def test_an_unwritten_result_is_not_ok() -> None:
    """Results with unrecognised or failed outcome have ok=False."""
    assert DownloadResult("unrecognised").ok is False
    assert DownloadResult("failed").ok is False


def test_download_result_defaults() -> None:
    """DownloadResult has sensible defaults for output fields."""
    r = DownloadResult("created")
    assert r.json_data == {}
    assert r.output_filename is None
    assert r.site_chapters == 0
    assert r.chapters_before == 0
    assert r.chapters_after == 0
    assert r.distinct_urls_after == 0
    assert r.added == 0
    assert r.updated == 0
    assert r.errored == 0
    assert r.error == ""


def test_download_result_is_frozen() -> None:
    """DownloadResult is immutable once created."""
    r = DownloadResult("created")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.outcome = "failed"  # type: ignore[misc]


def test_the_unrecognised_messages_say_nothing_was_changed() -> None:
    """Both UNRECOGNISED_MESSAGE and UNREADABLE_MESSAGE end with 'nothing was changed'."""
    assert UNRECOGNISED_MESSAGE.endswith("nothing was changed")
    assert UNREADABLE_MESSAGE.endswith("nothing was changed")
