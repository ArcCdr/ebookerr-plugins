"""Tests for EpubMergePlugin ( RB15)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ebookerr_sdk.spi as api
import pytest
from ebookerr_sdk.testing import Cancelled, FakeContext
from epub_merge.plugin import EpubMergePlugin


def _submitted(*ids: str) -> api.ViewResult:
    """The merge view submitted with every named orphan selected, in this order."""
    return api.ViewResult(
        submitted=True, selections={"orphans": api.ViewSelection(order=ids, selected=ids)}
    )


def _make_book_view(book_id: str, title: str = "Book", author: str = "Author") -> api.BookView:
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
    )


@pytest.fixture(autouse=True)
def _real_merge_target_files(tmp_path: Path, build_epub: Callable[..., Path]) -> None:
    """Stand in a minimal real EPUB at each conventional a/b/c/d.epub path this module fabricates.

    process() now opens the survivor's own ``epub_path`` as the merged file to read its
    chapter table (CHC-D9), even though ``merge_epubs`` is mocked throughout this module and
    never actually writes one. Idempotent: a test that already built a real file at one of
    these paths (via its own call) leaves it untouched.
    """
    for name in ("a.epub", "b.epub", "c.epub", "d.epub"):
        path = tmp_path / name
        if not path.exists():
            build_epub([("Chapter 1", "https://example.com/1")], filename=name)


def _make_item(
    book_id: str, epub_path: Path, title: str = "Book", author: str = "Author"
) -> api.EpubItem:
    """Build an EpubItem at *epub_path*, which the autouse fixture already staged as a real EPUB."""
    return api.EpubItem(book=_make_book_view(book_id, title, author), epub_path=epub_path)


def test_manifest_contract() -> None:
    manifest = EpubMergePlugin().manifest
    assert manifest.id == "epub_merge"
    assert manifest.plugin_type == api.PluginType.EPUB
    assert manifest.accepts_list is True
    assert manifest.headed is True
    assert manifest.headless is False
    assert manifest.priority == 50
    assert manifest.events == ()


def test_manifest_merge_trigger_requires_two_books() -> None:
    manifest = EpubMergePlugin().manifest
    assert manifest.ui_triggers[0].min_books == 2


def test_process_single_item_alerts_and_noops(tmp_path: Path) -> None:
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1")])
    item = _make_item("b1", tmp_path / "a.epub")

    patches = EpubMergePlugin().process((item,), ctx)

    assert patches == []
    assert ctx.alerts == ["Select at least two books to merge."]


def test_confirm_no_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=99)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert calls == []
    assert len(ctx.views) == 1


def test_headless_never_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=99)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext()
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert calls == []
    assert ctx.views == []


def test_headless_logs_the_skip(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Headless mode logs an INFO line explaining the merge was skipped."""
    ctx = FakeContext()
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process(items, ctx)

    assert "Merge skipped: headless invocations never merge" in caplog.text


def test_headed_requests_a_view(tmp_path: Path) -> None:
    """A headed call requests exactly one view."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert len(ctx.views) == 1


def test_view_has_survivor_picker_note_and_item_list(tmp_path: Path) -> None:
    """The requested view's sections are [FIELDS, NOTE, ITEM_LIST] in that order."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    assert [section.kind for section in view.sections] == [
        api.ViewSectionKind.FIELDS,
        api.ViewSectionKind.NOTE,
        api.ViewSectionKind.ITEM_LIST,
    ]


def test_note_is_survivor_neutral(tmp_path: Path) -> None:
    """The note's text does not mention any survivor and states the consequence exactly."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Tale 1-2"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    note = next(s for s in view.sections if s.kind == api.ViewSectionKind.NOTE)
    assert note.text == (
        "Every book you leave ticked is merged into the book above and then deleted. "
        "This cannot be undone."
    )
    assert "Tale 1-2" not in note.text


def test_item_list_includes_all_selected_books(tmp_path: Path) -> None:
    """The item list holds all three items, including the survivor."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    item_list = next(s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST)
    ids = {item.id for item in item_list.items}
    assert len(item_list.items) == 3
    assert ids == {"b1", "b2", "b3"}


def test_item_list_labels_are_titles(tmp_path: Path) -> None:
    """Each item's label is its book's title, including the survivor."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    item_list = next(s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST)
    labels = {item.id: item.label for item in item_list.items}
    assert labels == {"b1": "Book A", "b2": "Book B", "b3": "Book C"}


def test_item_list_sublabels_are_authors(tmp_path: Path) -> None:
    """Each item's sublabel is its book's author, including the survivor."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A", author="Author A"),
        _make_item("b2", tmp_path / "b.epub", "Book B", author="Author B"),
        _make_item("b3", tmp_path / "c.epub", "Book C", author="Author C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    item_list = next(s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST)
    sublabels = {item.id: item.sublabel for item in item_list.items}
    assert sublabels == {"b1": "Author A", "b2": "Author B", "b3": "Author C"}


def test_every_non_survivor_is_preselected(tmp_path: Path) -> None:
    """Every item, survivor included, is pre-selected; only the survivor is locked.

    SPI 2.4's `locked` means "a locked, SELECTED item can never be deselected" — a
    locked-but-unselected row fails the server's own validation, so the survivor's row
    (b1) must be selected=True like its siblings. Its checkbox still renders disabled
    (locked=True) and the merge itself always excludes the survivor from `others` by id,
    never by its selected state.
    """
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    item_list = next(s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST)
    assert {item.id for item in item_list.items} == {"b1", "b2", "b3"}
    selected_ids = {item.id for item in item_list.items if item.selected}
    assert selected_ids == {"b1", "b2", "b3"}


def test_the_merge_view_names_its_verb(tmp_path: Path) -> None:
    """The orphans item list has item_verb='Merge'."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    item_list = next(s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST)
    assert item_list.item_verb == "Merge"


def test_the_submit_label_names_the_plural_book_count(tmp_path: Path) -> None:
    """_build_view(survivor, [a, b, c]) returns submit_label == 'Merge 3 books'."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
        _make_item("b4", tmp_path / "d.epub", "Book D"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    assert view.submit_label == "Merge 3 books"


def test_the_submit_label_is_singular_for_one_book(tmp_path: Path) -> None:
    """_build_view(survivor, [a]) returns submit_label == 'Merge 1 book'."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    assert view.submit_label == "Merge 1 book"


def test_the_submit_label_is_not_a_bare_verb(tmp_path: Path) -> None:
    """Label starts with verb but is not a bare verb like 'Merge'."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    deny_set = {"delete", "clear", "remove", "confirm", "ok", "yes", "merge"}
    assert view.submit_label.split()[0].lower() in {"merge"}
    assert view.submit_label.lower() not in deny_set


def test_the_view_is_still_marked_dangerous(tmp_path: Path) -> None:
    """Regression guard: view.danger is True and title is unchanged."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    assert view.danger is True
    assert view.title == "Merge chapters"


def test_the_note_still_states_the_consequence(tmp_path: Path) -> None:
    """NOTE section contains 'This cannot be undone.' but no survivor's title."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    note = next(s for s in view.sections if s.kind == api.ViewSectionKind.NOTE)
    assert "This cannot be undone." in note.text
    # Note should not mention book titles (survivor-neutral)
    assert "Book A" not in note.text
    assert "Book B" not in note.text


def test_selected_subset_is_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A selection of only the second orphan merges [survivor, that_orphan] — not the third."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={"orphans": api.ViewSelection(order=("b2", "b3"), selected=("b3",))},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert len(calls) == 1
    assert calls[0][0] == tmp_path / "a.epub"
    assert calls[0][1] == [tmp_path / "c.epub"]


def test_selection_order_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The merge receives selected orphans in the order the view returned, after the survivor."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=3)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={
                    "orphans": api.ViewSelection(order=("b3", "b2"), selected=("b3", "b2"))
                },
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert len(calls) == 1
    assert calls[0][1] == [tmp_path / "c.epub", tmp_path / "b.epub"]


def test_empty_selection_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty selection cancels the merge; merge_epubs is never called."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={"orphans": api.ViewSelection(order=(), selected=())},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert calls == []


def test_empty_selection_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An empty selection logs an INFO cancellation line."""
    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={"orphans": api.ViewSelection(order=(), selected=())},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process(items, ctx)

    assert "Merge cancelled: no chapter selected" in caplog.text


def test_cancelled_view_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled (not submitted) view cancels the merge; merge_epubs is never called."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED, view_results=[api.ViewResult(submitted=False)]
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert calls == []


def test_confirmed_ui_context_is_ignored(tmp_path: Path) -> None:
    """The retired ui_context={'confirmed': '1'} back-channel no longer skips request_view."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED, ui_context={"confirmed": "1"})
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert len(ctx.views) == 1


def test_no_ask_yes_no_call(tmp_path: Path) -> None:
    """The fake context's ask_yes_no is never called."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED, dialog_answers=[True])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert ctx.dialog_answers == [True]


def test_manifest_is_unchanged() -> None:
    """accepts_list, headless, events, and the Merge trigger's min_books are unchanged."""
    manifest = EpubMergePlugin().manifest
    assert manifest.accepts_list is True
    assert manifest.headless is False
    assert manifest.events == ()
    assert manifest.ui_triggers[0].label == "Merge"
    assert manifest.ui_triggers[0].min_books == 2


def test_patches_shape_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=4)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    # Mock EpubDocument.open to return a document with matching title

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1", "b2", "b3")],
        dialog_answers=[True],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert len(calls) == 1
    assert calls[0][0] == tmp_path / "a.epub"
    assert calls[0][1] == [tmp_path / "b.epub", tmp_path / "c.epub"]
    assert ctx.dialog_answers == [True]
    assert len(ctx.views) == 1

    assert len(patches) == 3
    survivor = patches[0]
    assert survivor.book_id == "b1"
    assert survivor.fields == {"num_chapters": 4}
    assert survivor.emit_followup is False
    assert survivor.delete is False

    assert patches[1] == api.BookPatch(book_id="b2", delete=True, superseded_by=patches[0].book_id)
    assert patches[2] == api.BookPatch(book_id="b3", delete=True, superseded_by=patches[0].book_id)


def test_merge_error_alerts_and_returns_no_patches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If merge_epubs can't account for every source's chapters, the plugin must
    surface the reason to the user and return no patches — fail closed: the
    survivor is left untouched and nothing gets deleted."""
    from epub_merge.merge import EpubMergeError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise EpubMergeError('merging "b.epub" into "a.epub" copied 0 content chapter(s)')

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert len(ctx.alerts) == 1
    assert "Merge failed" in ctx.alerts[0]
    assert "copied 0 content chapter(s)" in ctx.alerts[0]


def test_manifest_declares_two_settings_fields() -> None:
    """Manifest declares exactly two settings fields for title page and book title rewrites."""
    manifest = EpubMergePlugin().manifest
    assert len(manifest.settings_schema.fields) == 2
    assert manifest.settings_schema.fields[0].key == "rewrite_title_page"
    assert manifest.settings_schema.fields[1].key == "rewrite_book_title"


def test_settings_fields_are_bool_with_false_default() -> None:
    """Both settings fields are bool type with False default, label, and help text."""
    manifest = EpubMergePlugin().manifest
    settings_fields = manifest.settings_schema.fields

    for settings_field in settings_fields:
        assert settings_field.type == "bool"
        assert settings_field.default is False
        assert settings_field.label
        assert len(settings_field.label) > 0
        assert settings_field.help
        assert len(settings_field.help) > 0


def test_settings_schema_method_matches_manifest() -> None:
    """The settings_schema() method returns the same schema as the manifest."""
    plugin = EpubMergePlugin()
    assert plugin.settings_schema() == plugin.manifest.settings_schema


def test_manifest_version_bumped() -> None:
    """Manifest version is bumped to 2.1.0."""
    manifest = EpubMergePlugin().manifest
    assert manifest.version == "2.1.0"


def test_process_passes_options_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass MergeOptions from settings to merge_epubs when rewrite flags are set."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=4)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    # Mock EpubDocument.open

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1", "b2")],
        settings={"rewrite_title_page": True, "rewrite_book_title": True},
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert len(calls) == 1
    assert calls[0][2] is not None
    assert calls[0][2].rewrite_title_page is True
    assert calls[0][2].rewrite_book_title is True


def test_process_defaults_options_when_settings_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default MergeOptions to False flags when settings are absent."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=4)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    # Mock EpubDocument.open

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")], settings={}
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert len(calls) == 1
    assert calls[0][2] is not None
    assert calls[0][2].rewrite_title_page is False
    assert calls[0][2].rewrite_book_title is False


def test_process_coerces_truthy_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Coerce truthy setting strings to bool via bool()."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=4)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    # Mock EpubDocument.open

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1", "b2")],
        settings={"rewrite_book_title": "true"},
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert len(calls) == 1
    assert calls[0][2] is not None
    assert calls[0][2].rewrite_book_title is True


def test_logs_info_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Log an INFO line at the start of a merge request with book count and survivor title."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        return MergeOutcome(chapter_count=5)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    # Mock EpubDocument.open to return a document with matching title

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "My Series"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process(items, ctx)

    assert "EPUB merge requested" in caplog.text
    assert "3" in caplog.text
    assert "My Series" in caplog.text


def test_logs_info_on_decline(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Log an INFO line when the user cancels the confirmation view."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process(items, ctx)

    assert "Merge cancelled: no chapter selected" in caplog.text
    assert "Book A" in caplog.text


def test_input_error_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MergeInputError produces a specific alert message."""
    from epub_merge.merge import MergeInputError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeInputError("b.epub: not a zip")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert len(ctx.alerts) == 1
    assert (
        ctx.alerts[0]
        == "Merge failed — one of the selected books could not be read: b.epub: not a zip"
    )


def test_content_error_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MergeContentError produces a specific alert message."""
    from epub_merge.merge import MergeContentError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeContentError("Only 2 of 3 chapters were accounted for")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert "Merge failed — the merged book did not account for every chapter:" in ctx.alerts[0]


def test_structure_error_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MergeStructureError produces a specific alert message."""
    from epub_merge.merge import MergeOptions, MergeStructureError

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeStructureError("Spine and manifest mismatch")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert ctx.alerts[0].startswith('Merge failed — "Book A" has an internal structure problem,')


def test_base_error_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare EpubMergeError produces a generic alert message."""
    from epub_merge.merge import EpubMergeError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise EpubMergeError("boom")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert ctx.alerts[0] == "Merge failed: boom"


def test_error_logs_exception_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Error logging includes the exception type name."""
    from epub_merge.merge import MergeInputError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeInputError("test error")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.ERROR):
        _ = EpubMergePlugin().process(items, ctx)

    assert "MergeInputError" in caplog.text


def test_structure_error_alert_says_retrying_will_not_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MergeStructureError message tells user retrying will not help."""
    from epub_merge.merge import MergeOptions, MergeStructureError

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeStructureError("Spine and manifest mismatch")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert "Retrying will not help." in ctx.alerts[0]
    assert "Run EPUB Validate on this book to see the details" in ctx.alerts[0]


def test_structure_error_alert_still_carries_the_technical_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MergeStructureError message includes the technical detail at the end."""
    from epub_merge.merge import MergeOptions, MergeStructureError

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeStructureError("Spine and manifest mismatch")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert "Technical detail: Spine and manifest mismatch" in ctx.alerts[0]


def test_structure_error_logs_exactly_one_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MergeStructureError logging emits exactly one ERROR log."""
    from epub_merge.merge import MergeOptions, MergeStructureError

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeStructureError("Spine and manifest mismatch")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.ERROR):
        _ = EpubMergePlugin().process(items, ctx)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert 'EPUB merge into "Book A" (book_id=b1) failed (MergeStructureError)' in caplog.text


def test_input_error_alert_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MergeInputError alert message remains unchanged."""
    from epub_merge.merge import MergeInputError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeInputError("b.epub: not a zip")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert (
        ctx.alerts[0]
        == "Merge failed — one of the selected books could not be read: b.epub: not a zip"
    )


def test_no_patches_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every failure case returns an empty list."""
    from epub_merge.merge import MergeContentError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeContentError("Something broke")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []


def test_success_patch_without_title_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the merged file title equals the survivor title, the patch has no title field."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=10)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open to return a document with matching title

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert len(patches) == 3
    survivor_patch = patches[0]
    assert survivor_patch.book_id == "b1"
    assert survivor_patch.fields == {"num_chapters": 10}
    assert "title" not in survivor_patch.fields


def test_success_patch_includes_renamed_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When merge_epubs returns a title, the patch includes it and logs the rename."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=7, title="My Series 1-7")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1", "b2", "b3")],
        settings={"rewrite_book_title": True},
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    with caplog.at_level(logging.INFO):
        patches = EpubMergePlugin().process(items, ctx)

    assert len(patches) == 3
    survivor_patch = patches[0]
    assert survivor_patch.fields == {"num_chapters": 7, "title": "My Series 1-7"}
    assert "EPUB merge renamed" in caplog.text


def test_a_merge_outcome_without_a_title_never_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When merge_epubs returns no title, the patch does not include title."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=4)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert len(patches) == 2
    survivor_patch = patches[0]
    assert "title" not in survivor_patch.fields
    assert survivor_patch.fields == {"num_chapters": 4}


def test_logs_info_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Log an INFO line on successful merge with chapter count."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=8)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open to return a document with matching title

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process(items, ctx)

    assert "EPUB merge applied" in caplog.text
    assert "8" in caplog.text or "chapter" in caplog.text


def test_merge_absorbs_other_story_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Survivor with no chapters, two others with story_url — survivor absorbs both URLs."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other1_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other1 = api.EpubItem(book=other1_view, epub_path=tmp_path / "b.epub")

    other2_view = api.BookView(
        book_id="b3",
        title="Three",
        author="Author",
        story_url="https://p/3",
        output_filename="test3.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other2 = api.EpubItem(book=other2_view, epub_path=tmp_path / "c.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process((survivor, other1, other2), ctx)

    assert len(patches) == 3
    survivor_patch = patches[0]
    assert survivor_patch.chapters == (
        api.ChapterLink(url="https://p/2", title="Two", ordinal=0),
        api.ChapterLink(url="https://p/3", title="Three", ordinal=0),
    )


def test_merge_keeps_survivor_chapters_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Survivor's chapters come first in the absorbed list."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/1", title="One"),),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert len(survivor_patch.chapters) == 2
    assert survivor_patch.chapters[0].url == "https://p/1"
    assert survivor_patch.chapters[1].url == "https://p/2"


def test_merge_absorbs_other_chapter_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Other's story_url and chapter_links are both absorbed."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/2a", title="Two-A"),),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert len(survivor_patch.chapters) == 2
    assert survivor_patch.chapters[0].url == "https://p/2"
    assert survivor_patch.chapters[0].title == "Two"
    assert survivor_patch.chapters[1].url == "https://p/2a"
    assert survivor_patch.chapters[1].title == "Two-A"


def test_merge_dedupes_repeated_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Repeated URLs are deduplicated; first occurrence kept."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=1)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/1", title="One"),),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Other",
        author="Author",
        story_url=None,
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/1", title="One"),),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert len(survivor_patch.chapters) == 1
    assert survivor_patch.chapters[0].url == "https://p/1"
    assert survivor_patch.chapters[0].title == "One"


def test_merge_skips_missing_story_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Other with None story_url only contributes its chapter links."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=1)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url=None,
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/2a", title="Two-A"),),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert len(survivor_patch.chapters) == 1
    assert survivor_patch.chapters[0].url == "https://p/2a"


def test_merge_absorption_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Re-merging the result as the survivor yields identical tuple."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    # First merge produces this result
    absorbed_chapters = (
        api.ChapterLink(url="https://p/1", title="One", ordinal=0),
        api.ChapterLink(url="https://p/2", title="Two", ordinal=0),
    )

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=absorbed_chapters,
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert survivor_patch.chapters == absorbed_chapters


def test_merge_logs_absorbed_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    build_epub: Callable[..., Path],
) -> None:
    """Logs absorbed chapter count with survivor info."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other1_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other1 = api.EpubItem(book=other1_view, epub_path=tmp_path / "b.epub")

    other2_view = api.BookView(
        book_id="b3",
        title="Three",
        author="Author",
        story_url="https://p/3",
        output_filename="test3.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other2 = api.EpubItem(book=other2_view, epub_path=tmp_path / "c.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process((survivor, other1, other2), ctx)

    assert "EPUB merge absorbed 2 chapter URL(s) into" in caplog.text
    assert "b1" in caplog.text


def test_merge_still_returns_delete_patches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete patches are still returned for merged-away books."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other1_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other1 = api.EpubItem(book=other1_view, epub_path=tmp_path / "b.epub")

    other2_view = api.BookView(
        book_id="b3",
        title="Three",
        author="Author",
        story_url="https://p/3",
        output_filename="test3.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other2 = api.EpubItem(book=other2_view, epub_path=tmp_path / "c.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    patches = EpubMergePlugin().process((survivor, other1, other2), ctx)

    assert len(patches) == 3
    assert patches[1] == api.BookPatch(book_id="b2", delete=True, superseded_by=patches[0].book_id)
    assert patches[2] == api.BookPatch(book_id="b3", delete=True, superseded_by=patches[0].book_id)


def test_merge_still_sets_num_chapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Survivor patch carries num_chapters; post-publish hook fires follow-up (EXP-202)."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=5)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert survivor_patch.fields["num_chapters"] == 5
    assert survivor_patch.emit_followup is False


def test_failed_merge_returns_no_patches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed merge returns no patches, no chapter absorption happens."""
    from epub_merge.merge import MergeContentError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise MergeContentError("Failed")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    assert patches == []


def test_declined_merge_returns_no_patches(tmp_path: Path) -> None:
    """User declines merge, returns no patches."""
    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    patches = EpubMergePlugin().process((survivor, other), ctx)

    assert patches == []


def test_merge_passes_book_urls_survivor_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """merge_epubs receives book_urls as keyword with survivor first."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None, dict[str, Any]]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options, {"book_urls": book_urls}))
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other1_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other1 = api.EpubItem(book=other1_view, epub_path=tmp_path / "b.epub")

    other2_view = api.BookView(
        book_id="b3",
        title="Three",
        author="Author",
        story_url="https://p/3",
        output_filename="test3.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other2 = api.EpubItem(book=other2_view, epub_path=tmp_path / "c.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    _ = EpubMergePlugin().process((survivor, other1, other2), ctx)

    assert len(calls) == 1
    assert calls[0][3]["book_urls"] == ["https://p/1", "https://p/2", "https://p/3"]


def test_merge_passes_none_for_missing_story_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """book_urls preserves None for missing story_url, maintaining positional alignment."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None, dict[str, Any]]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options, {"book_urls": book_urls}))
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other1_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url=None,
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other1 = api.EpubItem(book=other1_view, epub_path=tmp_path / "b.epub")

    other2_view = api.BookView(
        book_id="b3",
        title="Three",
        author="Author",
        story_url="https://p/3",
        output_filename="test3.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other2 = api.EpubItem(book=other2_view, epub_path=tmp_path / "c.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    _ = EpubMergePlugin().process((survivor, other1, other2), ctx)

    assert len(calls) == 1
    assert calls[0][3]["book_urls"] == ["https://p/1", None, "https://p/3"]


def test_merge_passes_book_urls_as_keyword(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """book_urls is passed as a keyword argument, not positional."""

    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[tuple[Path, ...], dict[str, Any]]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((tuple([target] + sources), {"book_urls": book_urls}))
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    _ = EpubMergePlugin().process((survivor, other), ctx)

    assert len(calls) == 1
    assert "book_urls" in calls[0][1]


def test_absorbed_includes_survivor_story_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Survivor's story_url is included in absorbed chapters."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    survivor_view = api.BookView(
        book_id="b1",
        title="One",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert survivor_patch.chapters == (
        api.ChapterLink(url="https://p/1", title="One", ordinal=0),
        api.ChapterLink(url="https://p/2", title="Two", ordinal=0),
    )


def test_absorbed_survivor_chapters_still_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Survivor's chapters come first, then its own story_url, then others."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/0", title="Zero"),),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert survivor_patch.chapters[0].url == "https://p/0"
    assert survivor_patch.chapters[1].url == "https://p/1"
    assert survivor_patch.chapters[2].url == "https://p/2"
    assert len(survivor_patch.chapters) == 3


def test_absorbed_dedupes_survivor_story_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """If survivor's story_url is already in its chapters, it appears only once."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=1)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/1", title="Old Title"),),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    patches = EpubMergePlugin().process((survivor,), ctx)

    # With only the survivor, and story_url already in chapters
    survivor_view2 = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/1", title="Old Title"),),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor2 = api.EpubItem(book=survivor_view2, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx2 = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor2, other), ctx2)

    survivor_patch = patches[0]
    # The URL https://p/1 should appear only once, with the first title seen
    urls = [c.url for c in survivor_patch.chapters]
    assert urls.count("https://p/1") == 1
    # The title should be from the chapter link, not the story_url
    assert survivor_patch.chapters[0].title == "Old Title"


def test_absorbed_skips_missing_survivor_story_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Survivor with story_url=None contributes nothing extra."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=1)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://p/0", title="Zero"),),
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Two",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    # Survivor's chapter + survivor's missing story_url (None, skipped) + other's story_url
    assert len(survivor_patch.chapters) == 2
    assert survivor_patch.chapters[0].url == "https://p/0"
    assert survivor_patch.chapters[1].url == "https://p/2"


def test_absorbed_still_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Re-merging the absorbed result yields identical tuple."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # First merge result
    first_absorbed = (
        api.ChapterLink(url="https://p/0", title="Zero", ordinal=0),
        api.ChapterLink(url="https://p/1", title="One", ordinal=0),
        api.ChapterLink(url="https://p/2", title="Two", ordinal=0),
    )

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url="https://p/1",
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=first_absorbed,
    )
    # Zero-chapter merged files: this test checks pure declared-chapters absorption (CHC-D13);
    # every candidate must be equally chapterless so the real, unmocked election still picks
    # book_id order rather than favouring whichever autouse-created file has more chapters.
    build_epub([], filename="a.epub")
    build_epub([], filename="b.epub")
    build_epub([], filename="c.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Other",
        author="Author",
        story_url="https://p/2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    assert survivor_patch.chapters == first_absorbed


def test_process_reports_staged_progress_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin reports staged progress at 5, 15, 80, 100 on success."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=7)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert [r[0] for r in ctx.reports] == pytest.approx([5.0, 15.0, 80.0, 100.0])


def test_process_reports_only_the_first_stage_for_a_short_selection(
    tmp_path: Path,
) -> None:
    """Single item returns immediately with only 5.0 reported."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1")])
    item = _make_item("b1", tmp_path / "a.epub")

    patches = EpubMergePlugin().process((item,), ctx)

    assert patches == []
    assert [r[0] for r in ctx.reports] == pytest.approx([5.0])


def test_process_stops_reporting_when_the_user_declines(
    tmp_path: Path,
) -> None:
    """User declines merge; reports 5.0 then stops."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert [r[0] for r in ctx.reports] == pytest.approx([5.0])


def test_process_does_not_report_full_progress_on_a_merge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merge error stops at stage 15.0, does not report 100."""
    from epub_merge.merge import EpubMergeError, MergeOptions

    def _raiser(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        raise EpubMergeError("boom")

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _raiser)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert [r[0] for r in ctx.reports] == pytest.approx([5.0, 15.0])


def test_process_logs_stage_start_at_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """After stage 15 is reported, a DEBUG line logs the merge start."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=9)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    with caplog.at_level(logging.DEBUG):
        _ = EpubMergePlugin().process(items, ctx)

    assert any(
        "EPUB merge stages: 3 book(s) into book_id=" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )


def test_merge_checks_cancellation_three_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-cancelling ctx double; run a successful two-book merge; assert ctx.cancel_checks == 3."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1", "b2")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert ctx.cancel_checks == 3


def test_merge_aborts_before_reading_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_after=0; assert Cancelled and ctx.reports collapse to [5.0]."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        cancel_after=0,
        view_results=[_submitted("b1", "b2")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with pytest.raises(Cancelled):
        _ = EpubMergePlugin().process(items, ctx)

    assert [r[0] for r in ctx.reports] == pytest.approx([5.0])


def test_merge_aborts_before_the_merge_begins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_after=1; assert Cancelled and ctx.reports collapse to [5.0, 15.0]."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        cancel_after=1,
        view_results=[_submitted("b1", "b2")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with pytest.raises(Cancelled):
        _ = EpubMergePlugin().process(items, ctx)

    assert [r[0] for r in ctx.reports] == pytest.approx([5.0, 15.0])


def test_merge_aborts_before_the_post_merge_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_after=2; assert Cancelled and ctx.reports collapse to [5.0, 15.0, 80.0]."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        cancel_after=2,
        view_results=[_submitted("b1", "b2")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with pytest.raises(Cancelled):
        _ = EpubMergePlugin().process(items, ctx)

    assert [r[0] for r in ctx.reports] == pytest.approx([5.0, 15.0, 80.0])


def test_merge_cancellation_does_not_return_patches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_after=1; assert Cancelled propagates rather than returning []."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        cancel_after=1,
        view_results=[_submitted("b1", "b2")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with pytest.raises(Cancelled):
        _ = EpubMergePlugin().process(items, ctx)


def test_uncancelled_merge_still_reports_all_four_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-cancelling double; assert ctx.reports collapse to [5.0, 15.0, 80.0, 100.0]."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1", "b2")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    assert [r[0] for r in ctx.reports] == pytest.approx([5.0, 15.0, 80.0, 100.0])


def test_merge_logs_the_armed_checkpoints(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """caplog.set_level(logging.DEBUG); non-cancelling double, two books; assert DEBUG message."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1", "b2")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.DEBUG):
        _ = EpubMergePlugin().process(items, ctx)

    assert any(
        record.getMessage() == "EPUB merge invoked for 2 book(s); cancellation checkpoints armed"
        for record in caplog.records
        if record.levelname == "DEBUG"
    )


def test_too_few_items_still_alerts_and_returns_empty(tmp_path: Path) -> None:
    """One item; assert the return is [] and ctx.alert was called."""
    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[_submitted("b1")],
        logger=logging.getLogger("plugin.epub_merge"),
    )
    item = _make_item("b1", tmp_path / "a.epub")

    patches = EpubMergePlugin().process((item,), ctx)

    assert patches == []
    assert ctx.alerts == ["Select at least two books to merge."]


def test_no_patch_from_a_merge_requests_a_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All merge patches request no follow-up (the post-publish hook fires it, EXP-202)."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=3)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    # Mock EpubDocument.open

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert all(p.emit_followup is False for p in patches)


def test_the_survivor_is_elected_not_taken_from_the_first_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invoking with [3-ch 9-11, 6-ch 1-6] produces a survivor BookPatch for the 6-ch book."""
    from epub_merge.merge import MergeOptions, MergeOutcome, SurvivorCandidate

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=9)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    def _candidate_from_epub(book_id: str, title: str, epub_path, created_at):
        if book_id == "b1":
            return SurvivorCandidate(
                book_id="b1",
                title=title,
                first_number=9,
                min_number=9,
                chapter_count=3,
                created_at=created_at,
            )
        else:
            return SurvivorCandidate(
                book_id="b2",
                title=title,
                first_number=1,
                min_number=1,
                chapter_count=6,
                created_at=created_at,
            )

    monkeypatch.setattr("epub_merge.plugin.candidate_from_epub", _candidate_from_epub)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book 9-11"),
        _make_item("b2", tmp_path / "b.epub", "Book 1-6"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert len(patches) >= 2
    assert patches[0].book_id == "b2"
    assert patches[1] == api.BookPatch(book_id="b1", delete=True, superseded_by=patches[0].book_id)


def test_the_same_selection_in_either_order_elects_the_same_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both [b1, b2] and [b2, b1] orderings elect the same survivor."""
    from epub_merge.merge import MergeOptions, MergeOutcome, SurvivorCandidate

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=9)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    def _candidate_from_epub(book_id: str, title: str, epub_path, created_at):
        if book_id == "b1":
            return SurvivorCandidate(
                book_id="b1",
                title=title,
                first_number=9,
                min_number=9,
                chapter_count=3,
                created_at=created_at,
            )
        else:
            return SurvivorCandidate(
                book_id="b2",
                title=title,
                first_number=1,
                min_number=1,
                chapter_count=6,
                created_at=created_at,
            )

    monkeypatch.setattr("epub_merge.plugin.candidate_from_epub", _candidate_from_epub)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items1 = (
        _make_item("b1", tmp_path / "a.epub", "Book 9-11"),
        _make_item("b2", tmp_path / "b.epub", "Book 1-6"),
    )
    patches1 = EpubMergePlugin().process(items1, ctx)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b2", "b1")])
    items2 = (
        _make_item("b2", tmp_path / "b.epub", "Book 1-6"),
        _make_item("b1", tmp_path / "a.epub", "Book 9-11"),
    )
    patches2 = EpubMergePlugin().process(items2, ctx)

    assert patches1[0].book_id == patches2[0].book_id == "b2"


def test_the_survivor_picker_defaults_to_the_elected_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The survivor picker defaults to the elected book_id (not named in prose)."""
    from epub_merge.merge import SurvivorCandidate

    def _candidate_from_epub(book_id: str, title: str, epub_path, created_at):
        if book_id == "b1":
            return SurvivorCandidate(
                book_id="b1",
                title=title,
                first_number=9,
                min_number=9,
                chapter_count=3,
                created_at=created_at,
            )
        else:
            return SurvivorCandidate(
                book_id="b2",
                title=title,
                first_number=1,
                min_number=1,
                chapter_count=6,
                created_at=created_at,
            )

    monkeypatch.setattr("epub_merge.plugin.candidate_from_epub", _candidate_from_epub)

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book 9-11"),
        _make_item("b2", tmp_path / "b.epub", "Book 1-6"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    survivor_section = next(
        (s for s in view.sections if s.kind == api.ViewSectionKind.FIELDS and s.name == "survivor"),
        None,
    )
    assert survivor_section is not None
    picker = survivor_section.fields[0]
    assert picker.default == "b2"


def test_the_confirmation_view_lists_every_other_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ITEM_LIST section holds all books, with the survivor locked."""
    from epub_merge.merge import SurvivorCandidate

    def _candidate_from_epub(book_id: str, title: str, epub_path, created_at):
        if book_id == "b1":
            return SurvivorCandidate(
                book_id="b1",
                title=title,
                first_number=9,
                min_number=9,
                chapter_count=3,
                created_at=created_at,
            )
        else:
            return SurvivorCandidate(
                book_id="b2",
                title=title,
                first_number=1,
                min_number=1,
                chapter_count=6,
                created_at=created_at,
            )

    monkeypatch.setattr("epub_merge.plugin.candidate_from_epub", _candidate_from_epub)

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book 9-11"),
        _make_item("b2", tmp_path / "b.epub", "Book 1-6"),
        _make_item("b3", tmp_path / "c.epub", "Book 3"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    item_list = next(s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST)
    ids = {item.id for item in item_list.items}
    assert ids == {"b1", "b2", "b3"}
    # The elected survivor (b2) is locked
    survivor_item = next(item for item in item_list.items if item.id == "b2")
    assert survivor_item.locked is True


def test_a_two_book_merge_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary two-book case still works with election."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    calls: list[tuple[Path, list[Path], MergeOptions | None]] = []

    def _recorder(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> int:
        calls.append((target, sources, options))
        return MergeOutcome(chapter_count=3)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _recorder)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert len(patches) == 2
    assert patches[1] == api.BookPatch(book_id="b2", delete=True, superseded_by=patches[0].book_id)
    assert len(calls) == 1


def test_a_too_small_selection_is_still_refused(tmp_path: Path) -> None:
    """One item alerts and returns [], with no election attempted."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1")])
    item = _make_item("b1", tmp_path / "a.epub")

    patches = EpubMergePlugin().process((item,), ctx)

    assert patches == []
    assert ctx.alerts == ["Select at least two books to merge."]


def test_a_shared_file_selection_is_still_refused(tmp_path: Path) -> None:
    """Two items with the same output_filename alert and return [], with no election attempted."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        api.EpubItem(
            book=api.BookView(
                book_id="b2",
                title="Book B",
                author="Author",
                story_url=None,
                output_filename="same.epub",
                num_chapters=None,
                status=None,
                rating=None,
                cover_ref=None,
                external=api.ExternalLink(),
                progress=api.ExternalProgress(),
                custom_values={},
            ),
            epub_path=tmp_path / "b.epub",
        ),
    )
    ctx.views = []

    items_tuple = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        items[1],
    )

    # Manually override the output_filename of the first item to match the second
    items_tuple_with_same_file = tuple(
        api.EpubItem(
            book=api.BookView(
                book_id=item.book.book_id,
                title=item.book.title,
                author=item.book.author,
                story_url=item.book.story_url,
                output_filename="same.epub" if i == 0 else item.book.output_filename,
                num_chapters=item.book.num_chapters,
                status=item.book.status,
                rating=item.book.rating,
                cover_ref=item.book.cover_ref,
                external=item.book.external,
                progress=item.book.progress,
                custom_values=item.book.custom_values,
            ),
            epub_path=item.epub_path,
        )
        for i, item in enumerate(items_tuple)
    )

    patches = EpubMergePlugin().process(items_tuple_with_same_file, ctx)

    assert patches == []
    assert len(ctx.alerts) == 1
    assert "share a file" in ctx.alerts[0]


def test_a_headless_invocation_still_merges_nothing(tmp_path: Path) -> None:
    """InvocationMode.HEADLESS returns []."""
    ctx = FakeContext()
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []
    assert ctx.views == []


def test_the_election_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At INFO level, one record matching 'Merge survivor elected:' is logged."""
    from epub_merge.merge import MergeOutcome, SurvivorCandidate

    def _candidate_from_epub(book_id: str, title: str, epub_path, created_at):
        return SurvivorCandidate(
            book_id=book_id,
            title=title,
            first_number=1 if book_id == "b1" else 5,
            min_number=1 if book_id == "b1" else 5,
            chapter_count=5,
            created_at=created_at,
        )

    monkeypatch.setattr("epub_merge.plugin.candidate_from_epub", _candidate_from_epub)

    def _merger(*a, **k):
        return MergeOutcome(chapter_count=5)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process(items, ctx)

    assert "Merge survivor elected:" in caplog.text


def test_the_view_offers_a_survivor_picker(tmp_path: Path) -> None:
    """The raised view has a FIELDS section named 'survivor' with one field keyed 'survivor'."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    survivor_section = next(
        (s for s in view.sections if s.kind == api.ViewSectionKind.FIELDS and s.name == "survivor"),
        None,
    )
    assert survivor_section is not None
    assert len(survivor_section.fields) == 1
    assert survivor_section.fields[0].key == "survivor"


def test_the_picker_is_locked_to_the_selection(tmp_path: Path) -> None:
    """The survivor picker has options_strict=True and options are the selected books' ids."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    survivor_section = next(
        (s for s in view.sections if s.kind == api.ViewSectionKind.FIELDS and s.name == "survivor"),
        None,
    )
    picker = survivor_section.fields[0]
    assert picker.options_strict is True
    assert set(picker.options) == {"b1", "b2", "b3"}


def test_the_picker_defaults_to_the_elected_survivor(tmp_path: Path) -> None:
    """The survivor picker's default is the elected book_id."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    survivor_section = next(
        (s for s in view.sections if s.kind == api.ViewSectionKind.FIELDS and s.name == "survivor"),
        None,
    )
    picker = survivor_section.fields[0]
    assert picker.default == "b1"


def test_the_picker_explains_what_surviving_means(tmp_path: Path) -> None:
    """The survivor picker's help text contains 'keeps its identity'."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    survivor_section = next(
        (s for s in view.sections if s.kind == api.ViewSectionKind.FIELDS and s.name == "survivor"),
        None,
    )
    picker = survivor_section.fields[0]
    assert "keeps its identity" in picker.help


def test_the_note_no_longer_names_a_survivor_in_prose(tmp_path: Path) -> None:
    """The NOTE section's text does not contain any book title."""
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Tale 1-2"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    note = next(s for s in view.sections if s.kind == api.ViewSectionKind.NOTE)
    assert "Tale 1-2" not in note.text
    assert "Book B" not in note.text
    assert "Book C" not in note.text


def test_the_survivor_row_is_locked_in_the_list(tmp_path: Path) -> None:
    """The ITEM_LIST contains the elected survivor with locked=True and selected=True.

    SPI 2.4's own contract: "a locked, SELECTED item can never be deselected" — a locked
    row that is NOT selected fails the server's own validation
    (plugin_view_wire.py's "locked item may not be deselected"), so every locked row,
    including the survivor's, must submit selected=True. The checkbox still renders
    disabled (locked=True), so the user cannot uncheck it either way, and the merge
    itself always excludes the survivor from `others` by id, never by its selected state.
    """
    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
        _make_item("b3", tmp_path / "c.epub", "Book C"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    item_list = next(s for s in view.sections if s.kind == api.ViewSectionKind.ITEM_LIST)
    survivor_item = next((item for item in item_list.items if item.id == "b1"), None)
    assert survivor_item is not None
    assert survivor_item.locked is True
    assert survivor_item.selected is True


def test_choosing_another_survivor_changes_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submitting survivor=<other book_id> makes that book the survivor in patches."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={
                    "orphans": api.ViewSelection(order=("b1", "b2"), selected=("b1", "b2"))
                },
                values={"survivor": {"survivor": "b2"}},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert len(patches) == 2
    survivor_patch = patches[0]
    assert survivor_patch.book_id == "b2"
    assert patches[1] == api.BookPatch(book_id="b1", delete=True, superseded_by=patches[0].book_id)


def test_an_unknown_survivor_choice_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submitting survivor='not-a-book' keeps the elected survivor."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=1)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={"orphans": api.ViewSelection(order=("b2",), selected=("b2",))},
                values={"survivor": {"survivor": "not-a-book"}},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert len(patches) == 2
    survivor_patch = patches[0]
    assert survivor_patch.book_id == "b1"
    assert patches[1] == api.BookPatch(book_id="b2", delete=True, superseded_by=patches[0].book_id)


def test_an_override_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An override logs an INFO record matching 'Merge survivor overridden by the user'."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=1)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={"orphans": api.ViewSelection(order=("b1",), selected=("b1",))},
                values={"survivor": {"survivor": "b2"}},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.INFO):
        _ = EpubMergePlugin().process(items, ctx)

    assert "Merge survivor overridden by the user" in caplog.text


def test_an_ignored_override_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An ignored override logs a WARNING record matching 'Merge survivor override ignored'."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=1)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={"orphans": api.ViewSelection(order=("b2",), selected=("b2",))},
                values={"survivor": {"survivor": "not-a-book"}},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    with caplog.at_level(logging.WARNING):
        _ = EpubMergePlugin().process(items, ctx)

    assert "Merge survivor override ignored" in caplog.text


def test_cancelling_still_merges_nothing(tmp_path: Path) -> None:
    """An unsubmitted result returns []."""
    ctx = FakeContext(
        mode=api.InvocationMode.HEADED, view_results=[api.ViewResult(submitted=False)]
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []


def test_clearing_the_list_still_merges_nothing(tmp_path: Path) -> None:
    """A submitted result with an empty orphans selection returns []."""
    ctx = FakeContext(
        mode=api.InvocationMode.HEADED,
        view_results=[
            api.ViewResult(
                submitted=True,
                selections={"orphans": api.ViewSelection(order=(), selected=())},
                values={"survivor": {"survivor": "b1"}},
            )
        ],
    )
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    assert patches == []


def test_an_overlapping_merge_alerts_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ctx.alert is called with a message about duplicate numbers."""
    from epub_merge.merge import MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: object | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=6, duplicate_numbers=("3",))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    # Should have called alert once
    assert len(ctx.alerts) == 1
    alert_msg = ctx.alerts[0]
    assert "appear more than once" in alert_msg
    assert "3" in alert_msg


def test_an_overlapping_merge_still_returns_its_patches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patches are returned unchanged; the alert does not abort the merge."""
    from epub_merge.merge import MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: object | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=6, duplicate_numbers=("3",))

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    patches = EpubMergePlugin().process(items, ctx)

    # Should return patches: one for survivor update, one for the deleted book
    assert len(patches) >= 2
    # First patch should be the survivor with num_chapters update
    assert patches[0].book_id == "b1"
    assert patches[0].fields == {"num_chapters": 6}
    # Find the delete patch
    delete_patches = [p for p in patches if p.delete]
    assert len(delete_patches) == 1
    assert delete_patches[0].book_id == "b2"


def test_a_clean_merge_raises_no_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ctx.alert is not called when duplicate_numbers is empty."""
    from epub_merge.merge import MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: object | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=6, duplicate_numbers=())

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    # Should not have called alert
    assert len(ctx.alerts) == 0


@pytest.mark.pins("EXP-271")
def test_a_completed_merge_announces_what_it_absorbed(caplog: Any) -> None:
    """_announce_outcome for a successful merge publishes a success toast and logs the result."""
    from epub_merge.plugin import _announce_outcome

    ctx = FakeContext()

    with caplog.at_level(logging.INFO):
        _announce_outcome(
            ctx,
            survivor_title="Alpha",
            chapters_gained=3,
            books_deleted=1,
            books_remaining=0,
            failed=0,
        )

    # Verify exactly one notify call with success kind
    assert len(ctx.notices) == 1
    kind, message, _ = ctx.notices[0]
    assert kind == "success"
    assert message == 'Merged 3 chapter(s) into "Alpha"; 1 book(s) deleted'

    # Verify info log contains the absorbed message
    logs_info = [r.message for r in caplog.records if r.levelname == "INFO"]
    absorbed_logs = [
        msg
        for msg in logs_info
        if 'Merge absorbed 3 chapter(s) into "Alpha"; 1 book(s) deleted' in msg
    ]
    assert len(absorbed_logs) > 0


@pytest.mark.pins("EXP-271")
def test_an_incomplete_merge_records_the_diagnostic(caplog: Any) -> None:
    """_announce_outcome for an incomplete merge logs the diagnostic and publishes no toast."""
    from epub_merge.plugin import _announce_outcome

    ctx = FakeContext()

    with caplog.at_level(logging.INFO):
        _announce_outcome(
            ctx,
            survivor_title="Alpha",
            chapters_gained=0,
            books_deleted=0,
            books_remaining=2,
            failed=1,
        )

    # Verify no notify call
    assert len(ctx.notices) == 0

    # Verify info log contains the diagnostic
    logs_info = [r.message for r in caplog.records if r.levelname == "INFO"]
    diagnostic_logs = [
        msg
        for msg in logs_info
        if "Merge did not complete: 2 book(s) still present, 1 failed" in msg
    ]
    assert len(diagnostic_logs) > 0


@pytest.mark.pins("EXP-271")
def test_the_merge_plugin_announces_its_own_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge plugin's process method announces its outcome via _announce_outcome."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=5)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2", "b3")])

    items = (
        _make_item("b1", tmp_path / "a.epub", "Survivor"),
        _make_item("b2", tmp_path / "b.epub", "Orphan 1"),
        _make_item("b3", tmp_path / "c.epub", "Orphan 2"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    # Verify at least one success notify was recorded
    success_toasts = [msg for kind, msg, _ in ctx.notices if kind == "success"]
    assert len(success_toasts) > 0, f"Expected success notify, got: {ctx.notices}"
    merged_toasts = [msg for msg in success_toasts if "Merged" in msg]
    assert len(merged_toasts) > 0, f"Expected 'Merged' in message: {success_toasts}"


def test_the_survivor_picker_shows_titles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The view's select field has option_labels showing titles, aligned with options."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    ctx = FakeContext(mode=api.InvocationMode.HEADED)
    items = (
        _make_item("b1", tmp_path / "a.epub", "Book A"),
        _make_item("b2", tmp_path / "b.epub", "Book B"),
    )

    _ = EpubMergePlugin().process(items, ctx)

    view = ctx.views[0]
    fields_section = next(s for s in view.sections if s.kind == api.ViewSectionKind.FIELDS)
    survivor_field = fields_section.fields[0]

    assert survivor_field.options == ("b1", "b2")
    assert survivor_field.option_labels == ("Book A", "Book B")


def test_chapter_links_carry_ordinals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Chapter links in the survivor patch carry ordinals."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)

    survivor_view = api.BookView(
        book_id="b1",
        title="Book A",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://x/1", title="Ch 1"),),
    )
    survivor = api.EpubItem(book=survivor_view, epub_path=tmp_path / "a.epub")

    other_view = api.BookView(
        book_id="b2",
        title="Book B",
        author="Author",
        story_url="https://x/story2",
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        chapters=(api.ChapterLink(url="https://x/2", title="Ch 2"),),
    )
    other = api.EpubItem(book=other_view, epub_path=tmp_path / "b.epub")

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    patches = EpubMergePlugin().process((survivor, other), ctx)

    survivor_patch = patches[0]
    # Check that chapters have ordinals
    assert len(survivor_patch.chapters) >= 2
    # First chapter should have ordinal 1
    assert survivor_patch.chapters[0].ordinal == 1
    # Story URL should have ordinal 0
    story_link = next((ch for ch in survivor_patch.chapters if ch.url == "https://x/story2"), None)
    assert story_link is not None
    assert story_link.ordinal == 0


def test_created_at_reaches_the_election(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_epub: Callable[..., Path]
) -> None:
    """Both items' created_at values are passed to candidate_from_epub for election."""
    from epub_merge.merge import MergeOptions, MergeOutcome

    candidates_seen: list[Any] = []

    def _merger(
        target: Path,
        sources: list[Path],
        options: MergeOptions | None = None,
        *,
        book_urls: list[str | None] | None = None,
    ) -> MergeOutcome:
        return MergeOutcome(chapter_count=2)

    def _election_spy(cands: list[Any]) -> str:
        candidates_seen.extend(cands)
        # Return first candidate's book_id
        return cands[0].book_id if cands else "unknown"

    monkeypatch.setattr("epub_merge.plugin.merge_epubs", _merger)
    monkeypatch.setattr("epub_merge.plugin.elect_survivor", _election_spy)

    survivor_view = api.BookView(
        book_id="b1",
        title="Older Book",
        author="Author",
        story_url=None,
        output_filename="test.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        created_at="2026-01-01T00:00:00Z",
    )
    epub_a = build_epub([("Chapter 1", "https://example.com/a1")], filename="a.epub")
    survivor = api.EpubItem(book=survivor_view, epub_path=epub_a)

    other_view = api.BookView(
        book_id="b2",
        title="Newer Book",
        author="Author",
        story_url=None,
        output_filename="test2.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=api.ExternalLink(),
        progress=api.ExternalProgress(),
        custom_values={},
        created_at="2026-01-02T00:00:00Z",
    )
    epub_b = build_epub([("Chapter 1", "https://example.com/b1")], filename="b.epub")
    other = api.EpubItem(book=other_view, epub_path=epub_b)

    ctx = FakeContext(mode=api.InvocationMode.HEADED, view_results=[_submitted("b1", "b2")])
    EpubMergePlugin().process((survivor, other), ctx)

    # Verify that created_at was passed to the candidates
    assert len(candidates_seen) >= 2
    assert any(c.created_at == "2026-01-01T00:00:00Z" for c in candidates_seen)
    assert any(c.created_at == "2026-01-02T00:00:00Z" for c in candidates_seen)


def test_epub_merge_declares_the_capability() -> None:
    """EpubMergePlugin declares handles_merge_proposals=True."""
    plugin = EpubMergePlugin()
    assert plugin.manifest.handles_merge_proposals is True
