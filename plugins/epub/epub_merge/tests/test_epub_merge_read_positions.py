"""Tests for read position preservation during EPUB merge (RP-D7)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.testing import FakeContext
from epub_merge.merge import MergeOutcome
from epub_merge.merge.model import MergedChapter
from epub_merge.plugin import EpubMergePlugin


def _submitted(*ids: str) -> api.ViewResult:
    """The merge view submitted with every named orphan selected, in this order."""
    return api.ViewResult(
        submitted=True, selections={"orphans": api.ViewSelection(order=ids, selected=ids)}
    )


_L = "https://example.test/s/"


def _make_mock_chapter_table(num_chapters: int) -> tuple:
    """Create a mock chapter table with the given number of chapters."""
    from types import SimpleNamespace

    return tuple(
        SimpleNamespace(
            ordinal=i + 1,
            key=f"https://x/{i + 1}",
            title=f"Ch {i + 1}",
            number=str(i + 1),
            href=f"chapter_{i + 1}.xhtml",
        )
        for i in range(num_chapters)
    )


def _make_mock_merge_outcome(num_chapters: int, contributions: tuple[int, ...]) -> MergeOutcome:
    """Create a MergeOutcome with proper chapter_map."""
    chapter_map = []
    chapter_idx = 0
    for input_idx, contrib in enumerate(contributions):
        for _ in range(contrib):
            chapter_map.append(
                MergedChapter(
                    input_index=input_idx,
                    source_href=f"chapter_{(chapter_idx % num_chapters) + 1}.xhtml",
                    filename=f"chapter_{chapter_idx + 1}.xhtml",
                )
            )
            chapter_idx += 1
    return MergeOutcome(
        chapter_count=num_chapters,
        contributions=contributions,
        chapter_map=tuple(chapter_map),
    )


def _make_book_view(
    book_id: str,
    title: str = "Book",
    author: str = "Author",
    read_position: api.ReadPosition | None = None,
) -> api.BookView:
    """Construct a BookView for testing."""
    return api.BookView(
        book_id=book_id,
        title=title,
        author=author,
        story_url=None,
        output_filename=f"{book_id}.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        read_position=read_position,
    )


def _make_item(
    book_id: str,
    epub_path: Path,
    title: str = "Book",
    author: str = "Author",
    read_position: api.ReadPosition | None = None,
) -> api.EpubItem:
    """Construct an EpubItem for testing."""
    return api.EpubItem(
        book=_make_book_view(book_id, title, author, read_position),
        epub_path=epub_path,
    )


def test_merge_carries_the_most_advanced_position(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three single-chapter inputs; only input 1 has position at ch1@0.4; expect ch2@0.4."""
    # Build three single-chapter EPUBs
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    # Mock merge_epubs to return a fixed outcome with chapter_map
    from types import SimpleNamespace

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(
            chapter_count=3,
            contributions=(1, 1, 1),
            chapter_map=(
                MergedChapter(0, "chapter_1.xhtml", "chapter_1.xhtml"),
                MergedChapter(1, "chapter_1.xhtml", "chapter_2.xhtml"),
                MergedChapter(2, "chapter_1.xhtml", "chapter_3.xhtml"),
            ),
        )

    def _mock_chapter_table(doc: object) -> tuple:
        return (
            SimpleNamespace(
                ordinal=1, key="https://x/1", title="Ch 1", number="1", href="chapter_1.xhtml"
            ),
            SimpleNamespace(
                ordinal=2, key="https://x/2", title="Ch 2", number="2", href="chapter_2.xhtml"
            ),
            SimpleNamespace(
                ordinal=3, key="https://x/3", title="Ch 3", number="3", href="chapter_3.xhtml"
            ),
        )

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)
    monkeypatch.setattr("epub_merge.plugin.chapter_table", _mock_chapter_table)

    # Create items: survivor has no position, input 1 has ch1@0.4, input 2 has no position
    items = (
        _make_item("b1", target, "Book A"),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=0.4,
                completed=False,
                chapter_href="chapter_1.xhtml",
                chapter_key="https://x/1",
            ),
        ),
        _make_item("b3", source2, "Book C"),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    # Find the read_position patch (not the main patch, not the delete patches)
    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) == 1

    # Input 1 ch1 -> merged ch2 (offset=1)
    assert read_pos_patches[0].read_position.chapter_index == 2
    assert read_pos_patches[0].read_position.chapter_progress == 0.4
    assert read_pos_patches[0].read_position.chapter_key == "https://x/2"
    assert read_pos_patches[0].read_position.chapter_href == "chapter_2.xhtml"
    assert read_pos_patches[0].read_position.chapter_title == "Ch 2"


def test_an_unplaceable_input_raises_the_inferred_notice(
    build_epub: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Input with no href/key -> notify with warning, WARNING logged."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    from types import SimpleNamespace

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(
            chapter_count=2,
            contributions=(1, 1),
            chapter_map=(
                MergedChapter(0, "chapter_1.xhtml", "chapter_1.xhtml"),
                MergedChapter(1, "chapter_1.xhtml", "chapter_2.xhtml"),
            ),
        )

    def _mock_chapter_table(doc: object) -> tuple:
        return (
            SimpleNamespace(
                ordinal=1, key="https://x/1", title="Ch 1", number="1", href="chapter_1.xhtml"
            ),
            SimpleNamespace(
                ordinal=2, key="https://x/2", title="Ch 2", number="2", href="chapter_2.xhtml"
            ),
        )

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)
    monkeypatch.setattr("epub_merge.plugin.chapter_table", _mock_chapter_table)

    items = (
        _make_item("b1", target, "Book A"),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=0.5,
                completed=False,
                chapter_href=None,
                chapter_key=None,
            ),
        ),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    with caplog.at_level(logging.WARNING):
        patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) == 1

    # Check that warning was logged
    assert "WARNING" in caplog.text or "by chapter count" in caplog.text

    # Check that the inferred-placement warning notification was added (alongside the merge's
    # own success notification, which this test is not about)
    warning_notifs = [n for n in ctx.notices if n[0] == "warning"]
    assert len(warning_notifs) == 1
    kind, msg, _ = warning_notifs[0]
    assert kind == "warning"
    assert "Book B" in msg
    assert "reading position" in msg.lower() or "chapter count" in msg.lower()


def test_positions_by_identity_raise_no_notice(
    build_epub: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Positions resolved by identity should not raise notice."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )

    from types import SimpleNamespace

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(
            chapter_count=2,
            contributions=(1, 1),
            chapter_map=(
                MergedChapter(0, "chapter_1.xhtml", "chapter_1.xhtml"),
                MergedChapter(1, "chapter_1.xhtml", "chapter_2.xhtml"),
            ),
        )

    def _mock_chapter_table(doc: object) -> tuple:
        return (
            SimpleNamespace(
                ordinal=1, key="https://x/1", title="Ch 1", number="1", href="chapter_1.xhtml"
            ),
            SimpleNamespace(
                ordinal=2, key="https://x/2", title="Ch 2", number="2", href="chapter_2.xhtml"
            ),
        )

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)
    monkeypatch.setattr("epub_merge.plugin.chapter_table", _mock_chapter_table)

    items = (
        _make_item("b1", target, "Book A"),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=0.5,
                completed=False,
                chapter_href="chapter_1.xhtml",
                chapter_key="https://x/1",
            ),
        ),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    with caplog.at_level(logging.WARNING):
        patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) == 1

    # Should not have warning notifications
    warning_notifs = [n for n in ctx.notices if n[0] == "warning"]
    assert len(warning_notifs) == 0


def test_first_input_read_lands_on_chapter_one(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only survivor (input 0) read (completed); expect ch1@1.0, not completed=True (3 total)."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return _make_mock_merge_outcome(3, (1, 1, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)
    monkeypatch.setattr("epub_merge.plugin.chapter_table", lambda doc: _make_mock_chapter_table(3))

    items = (
        _make_item(
            "b1",
            target,
            "Book A",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=1.0,
                completed=True,
            ),
        ),
        _make_item("b2", source1, "Book B"),
        _make_item("b3", source2, "Book C"),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) == 1

    # Input 0 ch1@completed -> merged ch1, but not marked completed (only 1/3 chapters)
    assert read_pos_patches[0].read_position.chapter_index == 1
    assert read_pos_patches[0].read_position.chapter_progress == 1.0
    assert read_pos_patches[0].read_position.completed is False


def test_all_inputs_read_marks_the_book_completed(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All three inputs completed=True; expect last patch marked completed=True."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=3, contributions=(1, 1, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)
    monkeypatch.setattr("epub_merge.plugin.chapter_table", lambda doc: _make_mock_chapter_table(3))

    items = (
        _make_item(
            "b1",
            target,
            "Book A",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=1.0,
                completed=True,
            ),
        ),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T11:00:00Z",
                chapter_index=1,
                chapter_progress=1.0,
                completed=True,
            ),
        ),
        _make_item(
            "b3",
            source2,
            "Book C",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T12:00:00Z",
                chapter_index=1,
                chapter_progress=1.0,
                completed=True,
            ),
        ),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) > 0

    # Last entry should be ch3@1.0 marked completed
    assert read_pos_patches[-1].read_position.chapter_index == 3
    assert read_pos_patches[-1].read_position.completed is True


def test_no_positions_emits_none(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No input has read_position; expect no read_position patches."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=3, contributions=(1, 1, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)

    items = (
        _make_item("b1", target, "Book A"),
        _make_item("b2", source1, "Book B"),
        _make_item("b3", source2, "Book C"),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) == 0


def test_positions_emitted_oldest_first(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inputs read at ch1, ch2, ch3; expect emitted patches in chapter order."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=3, contributions=(1, 1, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)

    items = (
        _make_item(
            "b1",
            target,
            "Book A",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=1.0,
                completed=False,
            ),
        ),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T11:00:00Z",
                chapter_index=1,
                chapter_progress=1.0,
                completed=False,
            ),
        ),
        _make_item(
            "b3",
            source2,
            "Book C",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T12:00:00Z",
                chapter_index=1,
                chapter_progress=1.0,
                completed=False,
            ),
        ),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    # Should be at most 5 (MERGED_HISTORY_LIMIT) but we have 3
    assert len(read_pos_patches) <= 5

    # Chapter indices should be strictly increasing
    chapter_indices = [p.read_position.chapter_index for p in read_pos_patches]
    assert chapter_indices == sorted(chapter_indices)
    assert len(chapter_indices) == len(set(chapter_indices))  # All unique


def test_position_patches_precede_delete_patches(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every read_position patch must appear before every delete patch."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=3, contributions=(1, 1, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)

    items = (
        _make_item(
            "b1",
            target,
            "Book A",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=0.5,
                completed=False,
            ),
        ),
        _make_item("b2", source1, "Book B"),
        _make_item("b3", source2, "Book C"),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    # Find indices of first read_position patch and first delete patch
    first_read_pos_idx = None
    first_delete_idx = None

    for i, patch in enumerate(patches):
        if patch.read_position is not None and first_read_pos_idx is None:
            first_read_pos_idx = i
        if patch.delete and first_delete_idx is None:
            first_delete_idx = i

    # At least one read_position patch should exist
    assert first_read_pos_idx is not None, "No read_position patches found"
    # At least one delete patch should exist
    assert first_delete_idx is not None, "No delete patches found"
    # read_position patches come first
    assert first_read_pos_idx < first_delete_idx


def test_total_chapters_is_the_merged_count(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each emitted read_position.total_chapters equals outcome.chapter_count."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2"), ("Book B Ch. 3", _L + "b3")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=6, contributions=(2, 3, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)

    items = (
        _make_item(
            "b1",
            target,
            "Book A",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=0.5,
                completed=False,
            ),
        ),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T11:00:00Z",
                chapter_index=2,
                chapter_progress=0.7,
                completed=False,
            ),
        ),
        _make_item("b3", source2, "Book C"),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) > 0

    for patch in read_pos_patches:
        assert patch.read_position.total_chapters == 6


def test_captured_at_preserved(
    build_epub: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Emitted read_position.captured_at equals source book's original value."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=3, contributions=(1, 1, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)
    monkeypatch.setattr("epub_merge.plugin.chapter_table", lambda doc: _make_mock_chapter_table(3))

    original_timestamp = "2026-05-15T14:30:45Z"
    items = (
        _make_item("b1", target, "Book A"),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at=original_timestamp,
                chapter_index=1,
                chapter_progress=0.5,
                completed=False,
            ),
        ),
        _make_item("b3", source2, "Book C"),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process(items, ctx)

    read_pos_patches = [p for p in patches if p.read_position is not None]
    assert len(read_pos_patches) == 1
    assert read_pos_patches[0].read_position.captured_at == original_timestamp


def test_logs_the_merged_landing(
    build_epub: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With read positions, log contains 'Merged read positions for' and 'chapter X at YY%'."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1"), ("Book A Ch. 2", _L + "a2")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1"), ("Book B Ch. 2", _L + "b2")],
        doc_title="Book B",
        filename="b.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=4, contributions=(2, 2))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)

    items = (
        _make_item(
            "b1",
            target,
            "Book A",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T10:00:00Z",
                chapter_index=1,
                chapter_progress=0.4,
                completed=False,
            ),
        ),
        _make_item(
            "b2",
            source1,
            "Book B",
            read_position=api.ReadPosition(
                captured_at="2026-01-01T11:00:00Z",
                chapter_index=1,
                chapter_progress=0.6,
                completed=False,
            ),
        ),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    with caplog.at_level(logging.INFO):
        EpubMergePlugin().process(items, ctx)

    assert "Merged read positions for" in caplog.text
    assert "chapter" in caplog.text
    assert "%" in caplog.text


def test_logs_when_nothing_to_merge(
    build_epub: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no positions, log contains 'No read positions to merge for'."""
    target = build_epub(
        [("Book A Ch. 1", _L + "a1")],
        doc_title="Book A",
        filename="a.epub",
    )
    source1 = build_epub(
        [("Book B Ch. 1", _L + "b1")],
        doc_title="Book B",
        filename="b.epub",
    )
    source2 = build_epub(
        [("Book C Ch. 1", _L + "c1")],
        doc_title="Book C",
        filename="c.epub",
    )

    def _mock_merge(*args: object, **kwargs: object) -> MergeOutcome:
        return MergeOutcome(chapter_count=3, contributions=(1, 1, 1))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _mock_merge)

    items = (
        _make_item("b1", target, "Book A"),
        _make_item("b2", source1, "Book B"),
        _make_item("b3", source2, "Book C"),
    )

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    with caplog.at_level(logging.INFO):
        EpubMergePlugin().process(items, ctx)

    assert "No read positions to merge for" in caplog.text
