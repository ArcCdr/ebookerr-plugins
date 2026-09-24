"""Tests for ``FanFicFarePull`` — the DB-free FanFicFare pull engine (D29).

No database, no repository: every test drives ``FanFicFarePull.pull``/``check_for_update``
directly with a fake gateway and an ``ebookerr_sdk.testing.FakeContext``, asserting on the
returned ``BookPatch``/``UpdateCheck`` and the context's recorded reports/state.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from ebookerr_sdk.domain.dates import parse_datetime
from ebookerr_sdk.domain.ids import make_book_id
from ebookerr_sdk.spi import BookPatch, ChapterLink, SourcePullError
from ebookerr_sdk.testing import FakeContext, make_book_view
from fanficfare_source.metadata import (
    chapter_links_from_fanficfare,
    fanficfare_json_to_book_fields,
)
from fanficfare_source.parser import extract_metadata_json
from fanficfare_source.protocol import DownloadResult
from fanficfare_source.pull import FanFicFarePull

URL = "https://www.literotica.com/s/the-12th-key"
AUTHOR_URL = "https://www.literotica.com/authors/gabthewriter/works/stories"
OUTPUT = "gabthewriter/The 12th Key.epub"

_META_SAME = {"numChapters": "1", "dateUpdated": "2026-05-19", "status": "In-Progress"}
_META_MORE = {"numChapters": "5", "dateUpdated": "2026-07-01", "status": "In-Progress"}


def fff_json(**overrides: Any) -> dict[str, Any]:
    """Build a FanFicFare-shaped metadata/download JSON payload, one field at a time."""
    base: dict[str, Any] = {
        "title": "The 12th Key",
        "author": "gabthewriter",
        "storyId": "the-12th-key",
        "storyUrl": URL,
        "sectionUrl": URL,
        "authorUrl": AUTHOR_URL,
        "category": "Erotic Horror",
        "eroticatags": "Horror",
        "numChapters": "1",
        "status": "In-Progress",
        "output_filename": OUTPUT,
    }
    base.update(overrides)
    return base


class FakeGateway:
    """A ``FanFicFareGateway`` double that writes a staged file, like the real CLI does."""

    def __init__(
        self,
        *,
        ok: bool = True,
        json_data: dict[str, Any] | None = None,
        output_filename: str | None = OUTPUT,
        error: str = "",
        meta: dict[str, Any] | None = None,
        write_file: bool = True,
    ) -> None:
        """Configure this call's canned metadata/download outcome."""
        self.ok = ok
        self.json_data = json_data if json_data is not None else fff_json()
        self.output_filename = output_filename
        self.error = error
        self.meta = meta
        self.write_file = write_file
        self.download_calls: list[tuple[str, Path]] = []
        self.fetch_metadata_calls = 0
        self.pinned_outputs: list[str | None] = []
        self.progress_calls: list[float] = []

    def fetch_metadata(
        self,
        url: str,
        *,
        timeout_s: int = 600,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        """Record the call and return the canned metadata."""
        self.fetch_metadata_calls += 1
        return self.meta

    def download(
        self,
        url: str,
        *,
        work_dir: Path,
        update_in_place: bool = True,
        force: bool = True,
        timeout_s: int = 3600,
        cancel_event: threading.Event | None = None,
        on_progress: Any = None,
        pinned_output: str | None = None,
    ) -> DownloadResult:
        """Record the call, replay any canned progress ticks, and stage a fake EPUB."""
        self.download_calls.append((url, work_dir))
        self.pinned_outputs.append(pinned_output)
        for elapsed in self.progress_calls:
            if on_progress is not None:
                on_progress(elapsed)
        if self.ok and self.write_file and self.output_filename:
            staged = work_dir / self.output_filename
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text("fake epub")
        return DownloadResult(
            ok=self.ok,
            json_data=self.json_data,
            output_filename=self.output_filename,
            was_update=False,
            error=self.error,
        )

    def is_available(self) -> bool:
        """Report the gateway as always available."""
        return True


def make_engine(gateway: FakeGateway | None = None, **kwargs: Any) -> FanFicFarePull:
    """Build a ``FanFicFarePull`` over a fake gateway, defaulting to a successful one."""
    return FanFicFarePull(gateway or FakeGateway(), **kwargs)


def stage_existing(work_dir: Path, relative: str) -> None:
    """Write a placeholder file at ``work_dir / relative``, as the core would have staged it."""
    path = work_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("existing epub")


def prior_view(
    *,
    num_chapters: int | None = 1,
    date_updated: str | None = "2026-05-19",
    output_filename: str | None = OUTPUT,
    book_id: str | None = None,
) -> Any:
    """Build a ``BookView`` prior with sane FanFicFare-pull defaults."""
    return make_book_view(
        book_id=book_id or make_book_id(URL),
        story_url=URL,
        output_filename=output_filename,
        num_chapters=num_chapters,
        date_updated=parse_datetime(date_updated) if date_updated else None,
    )


def progress_values(ctx: FakeContext) -> list[float]:
    """Extract the plain percentage sequence from a ``FakeContext``'s recorded reports."""
    return [pct for pct, _note, _detail in ctx.reports]


# ── check_for_update ──────────────────────────────────────────────────────────


def test_check_for_update_needs_update_when_more_remote_chapters() -> None:
    """check_for_update returns needs_update=True when remote has more chapters."""
    engine = make_engine(FakeGateway(meta=_META_MORE))
    check = engine.check_for_update(URL, prior=prior_view(), ctx=None)
    assert check.needs_update is True


def test_check_for_update_needs_update_when_meta_is_none() -> None:
    """When fetch_metadata is None for a new book, needs_update=False (fail-closed)."""
    engine = make_engine(FakeGateway(meta=None))
    check = engine.check_for_update(URL, prior=None, ctx=None)
    assert check.needs_update is False
    assert check.error == "metadata fetch failed"
    assert check.book_id is None


def test_check_for_update_meta_none_names_the_priors_book_id() -> None:
    """When metadata fetch fails for a known book, the check still names its book id."""
    prior = prior_view()
    engine = make_engine(FakeGateway(meta=None))
    check = engine.check_for_update(URL, prior=prior, ctx=None)
    assert check.needs_update is False
    assert check.error == "metadata fetch failed"
    assert check.book_id == prior.book_id


def test_check_for_update_meta_none_warns_when_prior_is_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A metadata-fetch failure for a known book logs a WARNING."""
    engine = make_engine(FakeGateway(meta=None))
    with caplog.at_level(logging.WARNING, logger="fanficfare_source.pull"):
        engine.check_for_update(URL, prior=prior_view(), ctx=None)
    assert any("Auto-Pull check failed for" in r.getMessage() for r in caplog.records)


def test_check_for_update_returns_gateway_meta_dict() -> None:
    """UpdateCheck.meta is the exact dict returned by the gateway."""
    gateway = FakeGateway(meta=_META_MORE)
    check = make_engine(gateway).check_for_update(URL, prior=prior_view(), ctx=None)
    assert check.meta is _META_MORE


def test_check_for_update_treats_unparseable_remote_chapters_as_needing_update() -> None:
    """meta without a usable numChapters can't confirm no-new-content, so it needs an update."""
    engine = make_engine(FakeGateway(meta={"dateUpdated": "2026-05-19", "status": "In-Progress"}))
    check = engine.check_for_update(URL, prior=prior_view(), ctx=None)
    assert check.needs_update is True


def test_fanficfare_check_writes_nothing_and_reports_status() -> None:
    """check_for_update writes nothing; it only reports the observed status field."""
    gateway = FakeGateway(
        meta={"numChapters": "1", "dateUpdated": "2026-05-19", "status": "Completed"}
    )
    check = make_engine(gateway).check_for_update(URL, prior=None, ctx=None)
    assert check.fields == {"status": "Completed"}
    assert gateway.fetch_metadata_calls == 1
    assert gateway.download_calls == []


def test_fanficfare_check_names_the_canonical_book_for_an_alias_url() -> None:
    """An alias URL whose metadata poll names a canonical storyUrl reports that book id."""
    canon = "https://www.literotica.com/s/they-also-serve-1"
    alias = canon + "?page=2"
    meta = {
        "numChapters": "1",
        "dateUpdated": "2026-05-19",
        "status": "In-Progress",
        "storyUrl": canon,
        "sectionUrl": canon,
    }
    check = make_engine(FakeGateway(meta=meta)).check_for_update(alias, prior=None, ctx=None)
    assert check.book_id == make_book_id(canon)


def test_fanficfare_meta_is_reused_from_state_within_900_seconds() -> None:
    """A second check_for_update within the TTL reuses cached state; past it, it re-fetches."""
    gateway = FakeGateway(meta=_META_SAME)
    engine = make_engine(gateway)
    clock = {"t": 0.0}
    ctx = FakeContext(clock=lambda: clock["t"])

    engine.check_for_update(URL, prior=None, ctx=ctx)
    assert gateway.fetch_metadata_calls == 1

    engine.check_for_update(URL, prior=None, ctx=ctx)
    assert gateway.fetch_metadata_calls == 1

    clock["t"] = 901.0
    engine.check_for_update(URL, prior=None, ctx=ctx)
    assert gateway.fetch_metadata_calls == 2


def test_check_for_update_without_ctx_never_caches() -> None:
    """With no ctx, every call fetches fresh — there is nowhere to cache to."""
    gateway = FakeGateway(meta=_META_SAME)
    engine = make_engine(gateway)
    engine.check_for_update(URL, prior=None, ctx=None)
    engine.check_for_update(URL, prior=None, ctx=None)
    assert gateway.fetch_metadata_calls == 2


# ── pull: fixture-driven complete patch ─────────────────────────────────────


def test_fanficfare_pull_returns_a_complete_patch(
    fanficfare_fixtures: Path, tmp_path: Path
) -> None:
    """A fresh pull returns one complete BookPatch built from a recorded fixture."""
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()
    payload = extract_metadata_json(stdout)
    assert payload is not None
    gateway = FakeGateway(json_data=payload, output_filename=OUTPUT)
    engine = make_engine(gateway)

    patch = engine.pull(URL, tmp_path, None, FakeContext())

    assert patch.upsert is True
    assert patch.book_id == make_book_id(payload["storyUrl"], payload["sectionUrl"])
    expected_fields = fanficfare_json_to_book_fields(payload)
    for key, value in expected_fields.items():
        assert patch.fields[key] == value
    assert patch.fields["output_filename"] == OUTPUT
    assert patch.fields["format"] == "epub"
    assert patch.fields["auto_pull"] == 0  # the recorded fixture's status is "Completed"
    assert "book_update_time" in patch.fields
    assert "last_check_time" in patch.fields
    assert patch.chapters == tuple(
        ChapterLink(url=u, title=t, ordinal=o) for u, t, o in chapter_links_from_fanficfare(payload)
    )


def test_fanficfare_new_completed_story_sets_auto_pull_zero(tmp_path: Path) -> None:
    """A brand-new story that's already Completed on first pull disables auto_pull."""
    gateway = FakeGateway(json_data=fff_json(status="Completed"))
    patch = make_engine(gateway).pull(URL, tmp_path, None, FakeContext())
    assert patch.fields["auto_pull"] == 0


def test_fanficfare_update_does_not_touch_auto_pull_while_in_progress(tmp_path: Path) -> None:
    """An update pull whose story is still In-Progress writes neither auto_pull nor check time."""
    prior = prior_view()
    gateway = FakeGateway(json_data=fff_json(status="In-Progress"))

    patch = make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert patch.upsert is True
    assert "auto_pull" not in patch.fields
    assert "last_check_time" not in patch.fields


def test_fanficfare_no_new_content_returns_a_skip_patch(tmp_path: Path) -> None:
    """No new remote content, staged file present -> a touched-but-unchanged skip patch."""
    prior = prior_view()
    stage_existing(tmp_path, OUTPUT)
    gateway = FakeGateway(meta=_META_SAME)

    patch = make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert patch == BookPatch(book_id=prior.book_id, fields={}, upsert=False)
    assert gateway.download_calls == []


def test_pull_download_failure_raises_source_pull_error(tmp_path: Path) -> None:
    """A gateway failure surfaces as SourcePullError with the gateway's own message."""
    gateway = FakeGateway(ok=False, error="unsupported site: x", write_file=False)
    with pytest.raises(SourcePullError, match="unsupported site"):
        make_engine(gateway).pull(URL, tmp_path, None, FakeContext())


def test_pull_verify_failure_raises_source_pull_error(tmp_path: Path) -> None:
    """A download that reports success but stages no file fails verification."""
    gateway = FakeGateway(write_file=False)
    with pytest.raises(SourcePullError, match="not verified"):
        make_engine(gateway).pull(URL, tmp_path, None, FakeContext())


def test_pull_persist_failure_raises_source_pull_error(tmp_path: Path) -> None:
    """A download whose JSON has no story URL can't derive a book id."""
    gateway = FakeGateway(json_data=fff_json(storyUrl="", sectionUrl=""))
    with pytest.raises(SourcePullError, match="could not persist"):
        make_engine(gateway).pull(URL, tmp_path, None, FakeContext())


def test_pull_downloads_when_no_new_content_but_staged_file_missing(tmp_path: Path) -> None:
    """When there's no new content but the staged copy is absent, the download proceeds."""
    prior = prior_view()
    gateway = FakeGateway(meta=_META_SAME)

    patch = make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert patch.upsert is True
    assert gateway.download_calls != []


def test_pull_logs_the_missing_epub_override(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the staged copy is missing, log 'Re-downloading' / 'no EPUB on disk', not 'Skipping'."""
    prior = prior_view()
    gateway = FakeGateway(meta=_META_SAME)

    with caplog.at_level(logging.INFO, logger="fanficfare_source.pull"):
        make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert "Re-downloading" in caplog.text
    assert "no EPUB on disk" in caplog.text
    assert "Skipping pull for" not in caplog.text


def test_pull_skips_when_no_new_content_and_staged_file_present(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the staged copy is present and there's no new content, log 'Skipping pull for'."""
    prior = prior_view()
    stage_existing(tmp_path, OUTPUT)
    gateway = FakeGateway(meta=_META_SAME)

    with caplog.at_level(logging.INFO, logger="fanficfare_source.pull"):
        patch = make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert patch.upsert is False
    assert gateway.download_calls == []
    assert "Skipping pull for" in caplog.text


def test_pull_treats_an_unset_output_filename_as_missing(tmp_path: Path) -> None:
    """When the prior has no output_filename at all, treat it as missing and download."""
    prior = prior_view(output_filename=None)
    gateway = FakeGateway(meta=_META_SAME)

    patch = make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert patch.upsert is True


def test_skip_log_formats_date_updated(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The skip log formats date_updated as 'YYYY-MM-DD HH:MM'."""
    prior = prior_view(num_chapters=1, date_updated="2026-02-02")
    stage_existing(tmp_path, OUTPUT)
    gateway = FakeGateway(meta={"numChapters": "1", "dateUpdated": "2026-02-02"})

    with caplog.at_level(logging.INFO, logger="fanficfare_source.pull"):
        make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert any(
        "dateUpdated 2026-02-02 00:00" in r.getMessage() and "no new content" in r.getMessage()
        for r in caplog.records
    )


def test_a_fanficfare_failure_is_logged_at_debug_not_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """FanFicFare download failure logs at DEBUG, not ERROR."""
    gateway = FakeGateway(ok=False, error="boom", write_file=False)

    with (
        caplog.at_level(logging.DEBUG, logger="fanficfare_source.pull"),
        pytest.raises(SourcePullError),
    ):
        make_engine(gateway).pull(URL, tmp_path, None, FakeContext())

    error_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and r.name == "fanficfare_source.pull"
    ]
    assert len(error_records) == 0
    debug_records = [
        r
        for r in caplog.records
        if r.levelname == "DEBUG" and r.name == "fanficfare_source.pull"
    ]
    assert any(
        "FanFicFare reported a failure for" in r.getMessage() and "boom" in r.getMessage()
        for r in debug_records
    )


# ── pinned output paths ─────────────────────────────────────────────────────


def test_an_existing_book_is_pulled_with_its_stored_path_pinned(tmp_path: Path) -> None:
    """An existing book's output_filename is passed as pinned_output to the gateway."""
    prior = prior_view(output_filename=OUTPUT)
    gateway = FakeGateway()

    make_engine(gateway).pull(URL, tmp_path, prior, FakeContext())

    assert gateway.pinned_outputs == ["gabthewriter/The 12th Key${formatext}"]


def test_a_new_book_gets_no_pin(tmp_path: Path) -> None:
    """A new book (no prior) is downloaded without pinned_output."""
    gateway = FakeGateway()

    make_engine(gateway).pull(URL, tmp_path, None, FakeContext())

    assert gateway.pinned_outputs == [None]


def test_a_new_books_decomposed_filename_is_stored_composed(tmp_path: Path) -> None:
    """When FanFicFare stages a decomposed-Unicode filename, it's normalised and re-staged."""
    composed = "Zoë/Café.epub"
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed  # sanity: the fixture really is decomposed
    gateway = FakeGateway(
        json_data=fff_json(output_filename=decomposed), output_filename=decomposed
    )

    patch = make_engine(gateway).pull(URL, tmp_path, None, FakeContext())

    assert patch.fields["output_filename"] == composed
    assert (tmp_path / patch.fields["output_filename"]).exists()


# ── EMA calibration (in ctx state) ──────────────────────────────────────────


def test_fanficfare_ema_is_kept_in_state(tmp_path: Path) -> None:
    """A pull that downloads new chapters calibrates avg_chapter_seconds into ctx state."""
    prior = prior_view(num_chapters=1)
    gateway = FakeGateway(json_data=fff_json(numChapters="5"))
    clock_values = iter([0.0, 18.0])
    ctx = FakeContext()

    make_engine(gateway, clock=lambda: next(clock_values)).pull(URL, tmp_path, prior, ctx)

    # downloaded=5-1=4, avg=4.0 default, fff_elapsed=18.0
    # new_avg = round(0.3*(18/4) + 0.7*4.0, 2) = round(1.35+2.8, 2) = 4.15
    assert ctx.state_get("avg_chapter_seconds") == pytest.approx(4.15)


def test_ema_not_updated_when_no_new_chapters(tmp_path: Path) -> None:
    """EMA is untouched when the downloaded JSON shows no chapter growth over the prior."""
    prior = prior_view(num_chapters=6, date_updated="2026-05-19")
    gateway = FakeGateway(
        json_data=fff_json(numChapters="6"),
        meta={"numChapters": "7", "dateUpdated": "2026-07-01", "status": "In-Progress"},
    )
    ctx = FakeContext()

    make_engine(gateway).pull(URL, tmp_path, prior, ctx)

    assert ctx.state_get("avg_chapter_seconds") is None


def test_ema_skipped_when_chapter_count_unparseable(tmp_path: Path) -> None:
    """When the downloaded JSON lacks a parseable numChapters, EMA calibration is skipped."""
    gateway = FakeGateway(json_data=fff_json(numChapters=None))
    ctx = FakeContext()

    make_engine(gateway).pull(URL, tmp_path, None, ctx)

    assert ctx.state_get("avg_chapter_seconds") is None


def test_build_ticker_falls_back_to_default_avg_on_invalid_state(tmp_path: Path) -> None:
    """An unparseable avg_chapter_seconds state value falls back to the 4.0 default."""
    prior = prior_view(num_chapters=2, date_updated="2026-01-01")
    ctx = FakeContext()
    ctx.state_set("avg_chapter_seconds", "not-a-number")
    gateway = FakeGateway(
        json_data=fff_json(numChapters="6"),
        meta={"numChapters": "6", "dateUpdated": "2026-07-01", "status": "In-Progress"},
    )
    clock_values = iter([0.0, 8.0])

    make_engine(gateway, clock=lambda: next(clock_values)).pull(URL, tmp_path, prior, ctx)

    # avg fell back to AVG_CHAPTER_SECONDS_DEFAULT (4.0) instead of raising on "not-a-number".
    # downloaded=6-2=4; new_avg = round(0.3*(8/4) + 0.7*4.0, 2) = round(0.6+2.8, 2) = 3.4
    assert ctx.state_get("avg_chapter_seconds") == pytest.approx(3.4)


def test_fanficfare_state_refusal_does_not_fail_the_pull(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ctx that refuses state_set with ValueError still lets the pull complete."""

    class _RefusingContext(FakeContext):
        def state_set(self, key: str, value: Any, *, ttl_s: int | None = None) -> None:
            raise ValueError("state refused")

    # numChapters=None keeps EMA calibration from running at all, isolating this test to the
    # one state_set call the engine actually guards: caching the freshly-fetched metadata.
    gateway = FakeGateway(json_data=fff_json(numChapters=None), meta=_META_SAME)
    ctx = _RefusingContext()

    with caplog.at_level(logging.DEBUG, logger="fanficfare_source.pull"):
        patch = make_engine(gateway).pull(URL, tmp_path, None, ctx)

    assert patch.upsert is True
    assert any(
        "FanFicFare metadata for" in r.getMessage() and "not cached" in r.getMessage()
        for r in caplog.records
    )


# ── no-new-content predicate ────────────────────────────────────────────────


def test_no_new_content_true_when_date_matches() -> None:
    """_no_new_content is True when remote chapters/date match the prior."""
    prior = prior_view(num_chapters=3, date_updated="2026-02-02")
    meta = {"dateUpdated": "2026-02-02", "numChapters": "3"}
    assert FanFicFarePull._no_new_content(prior, meta) is True


def test_no_new_content_true_across_date_shapes() -> None:
    """_no_new_content handles different remote date string shapes."""
    prior = prior_view(num_chapters=3, date_updated="2026-02-02")
    meta = {"dateUpdated": "2026-02-02T00:00:00Z", "numChapters": "3"}
    assert FanFicFarePull._no_new_content(prior, meta) is True


def test_no_new_content_false_when_date_differs() -> None:
    """_no_new_content is False when the remote date_updated differs."""
    prior = prior_view(num_chapters=3, date_updated="2026-02-02")
    meta = {"dateUpdated": "2026-03-03", "numChapters": "3"}
    assert FanFicFarePull._no_new_content(prior, meta) is False


def test_no_new_content_false_when_remote_has_more_chapters() -> None:
    """_no_new_content is False when the remote has more chapters than the prior."""
    prior = prior_view(num_chapters=3, date_updated="2026-02-02")
    meta = {"dateUpdated": "2026-02-02", "numChapters": "4"}
    assert FanFicFarePull._no_new_content(prior, meta) is False


def test_no_new_content_false_when_date_unparseable() -> None:
    """_no_new_content is False (fail-open to a re-pull) when the remote date is unparseable."""
    prior = prior_view(num_chapters=3, date_updated="2026-02-02")
    meta = {"dateUpdated": "sometime", "numChapters": "3"}
    assert FanFicFarePull._no_new_content(prior, meta) is False


# ── progress reporting ──────────────────────────────────────────────────────


def test_progress_band_constants() -> None:
    """Assert the progress band constants have their target values."""
    from fanficfare_source.pull import (
        _TICK_LINEAR_FRACTION,
        FFF_PROGRESS_END,
        FFF_PROGRESS_START,
    )

    assert FFF_PROGRESS_START == 5.0
    assert FFF_PROGRESS_END == 92.0
    assert _TICK_LINEAR_FRACTION == 0.9


def test_ticker_is_linear_up_to_the_estimate(tmp_path: Path) -> None:
    """Ticker is linear: at half the estimate, report half of the linear fraction of the band."""
    prior = prior_view(num_chapters=4, date_updated="2026-01-01")
    # remote has 10 chapters -> to_download = 10-4 = 6, estimate_s = max(10, 6*4.0) = 24.0
    # tick(12.0) = elapsed at half the estimate -> fraction = 0.9 * (12/24) = 0.45
    # report = 5.0 + 87.0 * 0.45 = 5.0 + 39.15 = 44.15
    gateway = FakeGateway(
        json_data=fff_json(numChapters="10"),
        meta={"numChapters": "10", "dateUpdated": "2026-07-01", "status": "In-Progress"},
    )
    gateway.progress_calls = [12.0]
    ctx = FakeContext()

    patch = make_engine(gateway).pull(URL, tmp_path, prior, ctx)

    assert patch.upsert is True
    between = [p for p in progress_values(ctx) if 5.0 < p < 92.0]
    assert between == [pytest.approx(44.15)]


def test_ticker_hits_the_linear_fraction_at_the_estimate(tmp_path: Path) -> None:
    """At the estimate time, the ticker hits 0.9 of the band."""
    prior = prior_view(num_chapters=4, date_updated="2026-01-01")
    # estimate_s = 24.0; tick(24.0) -> fraction = 0.9 -> report = 5.0 + 87.0*0.9 = 83.3
    gateway = FakeGateway(
        json_data=fff_json(numChapters="10"),
        meta={"numChapters": "10", "dateUpdated": "2026-07-01", "status": "In-Progress"},
    )
    gateway.progress_calls = [24.0]
    ctx = FakeContext()

    patch = make_engine(gateway).pull(URL, tmp_path, prior, ctx)

    assert patch.upsert is True
    between = [p for p in progress_values(ctx) if 5.0 < p < 92.0]
    assert between == [pytest.approx(83.3)]


def test_ticker_keeps_creeping_past_the_estimate(tmp_path: Path) -> None:
    """Past the estimate, the ticker creeps asymptotically: increasing, never reaching 92."""
    prior = prior_view(num_chapters=4, date_updated="2026-01-01")
    gateway = FakeGateway(
        json_data=fff_json(numChapters="10"),
        meta={"numChapters": "10", "dateUpdated": "2026-07-01", "status": "In-Progress"},
    )
    gateway.progress_calls = [24.0, 48.0, 72.0]
    ctx = FakeContext()

    patch = make_engine(gateway).pull(URL, tmp_path, prior, ctx)

    assert patch.upsert is True
    between = [p for p in progress_values(ctx) if 5.0 < p < 92.0]
    assert len(between) == 3
    assert between[0] < between[1] < between[2]
    assert between[2] == pytest.approx(89.825, abs=0.1)


def test_ticker_never_reaches_the_band_end(tmp_path: Path) -> None:
    """Ticker asymptotically approaches but never reaches 92."""
    prior = prior_view(num_chapters=4, date_updated="2026-01-01")
    gateway = FakeGateway(
        json_data=fff_json(numChapters="10"),
        meta={"numChapters": "10", "dateUpdated": "2026-07-01", "status": "In-Progress"},
    )
    gateway.progress_calls = [24.0, 240.0, 2400.0]
    ctx = FakeContext()

    patch = make_engine(gateway).pull(URL, tmp_path, prior, ctx)

    assert patch.upsert is True
    between = [p for p in progress_values(ctx) if 5.0 < p < 92.0]
    assert all(p < 92.0 for p in between)


def test_ticker_is_monotone_over_many_ticks(tmp_path: Path) -> None:
    """Ticker is strictly increasing (monotone) over many elapsed times."""
    prior = prior_view(num_chapters=4, date_updated="2026-01-01")
    gateway = FakeGateway(
        json_data=fff_json(numChapters="10"),
        meta={"numChapters": "10", "dateUpdated": "2026-07-01", "status": "In-Progress"},
    )
    gateway.progress_calls = [1.0, 5.0, 12.0, 24.0, 30.0, 60.0, 600.0]
    ctx = FakeContext()

    patch = make_engine(gateway).pull(URL, tmp_path, prior, ctx)

    assert patch.upsert is True
    between = [p for p in progress_values(ctx) if 5.0 < p < 92.0]
    assert len(between) == 7
    for i in range(len(between) - 1):
        assert between[i] < between[i + 1]


def test_ticker_exists_without_a_remote_chapter_count(tmp_path: Path) -> None:
    """When metadata has no numChapters, the ticker still reports progress (not None)."""
    gateway = FakeGateway(json_data=fff_json(numChapters="1"), meta={})
    gateway.progress_calls = [5.0]
    ctx = FakeContext()

    patch = make_engine(gateway).pull(URL, tmp_path, None, ctx)

    assert patch.upsert is True
    between = [p for p in progress_values(ctx) if 5.0 < p < 92.0]
    assert len(between) >= 1


def test_ticker_without_metadata_uses_the_floor_estimate(tmp_path: Path) -> None:
    """When metadata has no chapter count, the ticker uses MIN_ESTIMATE_SECONDS (10.0)."""
    gateway = FakeGateway(json_data=fff_json(numChapters="1"), meta={})
    gateway.progress_calls = [10.0]
    ctx = FakeContext()

    patch = make_engine(gateway).pull(URL, tmp_path, None, ctx)

    assert patch.upsert is True
    # tick(10.0) with estimate 10.0 -> fraction = 0.9 -> report = 83.3
    between = [p for p in progress_values(ctx) if 5.0 < p < 92.0]
    assert between == [pytest.approx(83.3)]


def test_ticker_without_metadata_logs_the_floor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When metadata has no chapter count, a DEBUG log records the fallback to the floor."""
    caplog.set_level(logging.DEBUG)
    gateway = FakeGateway(json_data=fff_json(numChapters="1"), meta={})
    gateway.progress_calls = [5.0]

    make_engine(gateway).pull(URL, tmp_path, None, FakeContext())

    assert any(
        "No remote chapter count for the pull estimate; ticking against the 10s floor"
        in r.getMessage()
        for r in caplog.records
    )


def test_post_download_milestones(tmp_path: Path) -> None:
    """A successful pull reaches the post-download milestones: 92, 95, 97, 100."""
    ctx = FakeContext()

    patch = make_engine(FakeGateway()).pull(URL, tmp_path, None, ctx)

    assert patch.upsert is True
    progress = progress_values(ctx)
    assert 92.0 in progress
    assert 95.0 in progress
    assert 97.0 in progress
    assert 100.0 in progress
    idx_92 = progress.index(92.0)
    idx_95 = progress.index(95.0)
    idx_97 = progress.index(97.0)
    idx_100 = progress.index(100.0)
    assert idx_92 < idx_95 < idx_97 < idx_100


def test_seventy_is_no_longer_a_milestone(tmp_path: Path) -> None:
    """The old 70.0 milestone is no longer reported."""
    ctx = FakeContext()
    patch = make_engine(FakeGateway()).pull(URL, tmp_path, None, ctx)
    assert patch.upsert is True
    assert 70.0 not in progress_values(ctx)


def test_progress_ends_at_one_hundred(tmp_path: Path) -> None:
    """Pull progress sequence ends at 100.0."""
    ctx = FakeContext()
    patch = make_engine(FakeGateway()).pull(URL, tmp_path, None, ctx)
    assert patch.upsert is True
    assert progress_values(ctx)[-1] == 100.0


# ── misc ─────────────────────────────────────────────────────────────────────


def test_module_name_is_pull() -> None:
    """Pin that the module holds the DB-free FanFicFarePull engine, not a *Service class."""
    import importlib

    import fanficfare_source.pull as m

    assert m.FanFicFarePull.__name__ == "FanFicFarePull"

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.services.download_service")
