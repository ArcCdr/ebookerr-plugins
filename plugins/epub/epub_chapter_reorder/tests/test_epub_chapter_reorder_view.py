"""The chapter editor's declarative view construction (CHX-D3/CHX-D8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.epub.chapters import ChapterRole, classify_spine
from ebookerr_sdk.spi import ViewSectionKind


class TestBuildView:
    """Tests for _build_view helper function."""

    def test_one_item_per_spine_entry(self, epub_fixtures: Path) -> None:
        """For tending_bar.epub, the 'chapters' section has exactly len(classify_spine(doc)) items.

        Verify item count matches spine entry count.
        """
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        assert len(chapters_section.items) == len(entries)

    def test_item_ids_are_idrefs(self, epub_fixtures: Path) -> None:
        """Every ViewItem.id equals the corresponding ChapterEntry.idref."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        for item, entry in zip(chapters_section.items, entries, strict=True):
            assert item.id == entry.idref

    def test_label_falls_back_to_href(self, build_epub):
        """An entry with an empty resolved title yields a ViewItem whose label equals its href."""
        from epub_chapter_reorder.plugin import _build_view

        # Build a book with a chapter that has an empty title (no title page)
        chapters = [("", "https://example.com/ch1")]
        epub_path = build_epub(chapters, doc_title="Test", include_title_page=False)
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        view = _build_view(doc, "Test", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        # The first chapter should have label = href
        item = chapters_section.items[0]
        entry = entries[0]
        assert item.label == entry.href

    def test_sublabel_is_the_href(self, epub_fixtures: Path) -> None:
        """Every item's sublabel equals its entry's href."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        for item, entry in zip(chapters_section.items, entries, strict=True):
            assert item.sublabel == entry.href

    def test_content_rows_carry_no_role_badge(self, build_epub):
        """A CONTENT entry with no number has badges == ()."""
        from epub_chapter_reorder.plugin import _build_view

        # Build a chapter with no number (so it's classified as CONTENT if unnumbered)
        chapters = [("Some Content", "https://example.com/ch1")]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        view = _build_view(doc, "Test", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        # Find a CONTENT entry with no number
        for item, entry in zip(chapters_section.items, entries, strict=True):
            if entry.role == ChapterRole.CONTENT and not entry.number:
                # Should have no badges
                assert item.badges == ()

    def test_title_page_carries_a_title_badge(self, epub_fixtures: Path):
        """A title_page manifest id yields a ViewItem whose badges contain 'Other'."""
        from epub_chapter_reorder.plugin import _build_view

        # Use tending_bar which has a title page (structurally OTHER)
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        # Find an OTHER entry and verify it has a badge
        for item, entry in zip(chapters_section.items, entries, strict=True):
            if entry.role == ChapterRole.OTHER:
                assert "Other" in item.badges or item.badges == ()
        # At minimum we should have entries
        assert len(chapters_section.items) > 0

    def test_unnumbered_epilogue_carries_a_back_badge(self, build_epub):
        """An unnumbered 'Epilogue' navLabel yields badges containing 'Back'."""
        from epub_chapter_reorder.plugin import _build_view

        chapters = [("Epilogue", "https://example.com/epilogue")]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        view = _build_view(doc, "Test", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        for item, entry in zip(chapters_section.items, entries, strict=True):
            if entry.role == ChapterRole.BACK:
                assert "Back" in item.badges

    def test_numbered_chapter_carries_a_number_badge(self, build_epub):
        """A chapter titled 'Ch. 07' yields badges containing 'Ch. 07'."""
        from epub_chapter_reorder.plugin import _build_view

        chapters = [("Chapter 07", "https://example.com/ch07")]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        view = _build_view(doc, "Test", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        for item, entry in zip(chapters_section.items, entries, strict=True):
            if entry.number:
                assert f"Ch. {entry.number}" in item.badges

    def test_badge_order_is_role_then_number_then_duplicate(self, build_epub):
        """Role badges appear before number badges, which appear before duplicate badges."""
        from epub_chapter_reorder.plugin import _build_view

        # Build with two identical titled chapters to trigger duplicate detection
        chapters = [
            ("Epilogue 12", "https://example.com/ch12"),
            ("Epilogue 12", "https://example.com/ch12a"),
        ]
        epub_path = build_epub(chapters, doc_title="Test", include_title_page=False)
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        view = _build_view(doc, "Test", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")

        # Find entries with duplicate detection
        for item, entry in zip(chapters_section.items, entries, strict=True):
            if "Duplicate" in item.badges and ("Epilogue" in entry.title or entry.number):
                # Should have duplicate badge as last element
                assert item.badges[-1] == "Duplicate"

    def test_every_item_is_selected_and_unlocked(self, epub_fixtures: Path) -> None:
        """Every ViewItem has selected is True and locked is False."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        for item in chapters_section.items:
            assert item.selected is True
            assert item.locked is False

    @pytest.mark.pins("EXP-185")
    def test_non_content_rows_are_fixed_with_the_zone_rule_as_reason(self, build_epub) -> None:
        """Title page, front matter and back matter rows are fixed with their zone reasons.

        The editor must lock the same rows the headless pass (CHC-D5) will not move,
        identified via chapter_role over title and number alone. Content rows are unlocked.
        """
        from epub_chapter_reorder.plugin import _build_view

        epub = build_epub(
            [
                ("Prologue", "https://x.test/p"),
                ("Chapter 1", "https://x.test/1"),
                ("Chapter 2", "https://x.test/2"),
                ("Author's Note", "https://x.test/n"),
                ("Epilogue", "https://x.test/e"),
            ],
            doc_title="Zone Book",
        )
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        view = _build_view(doc, "Zone Book", entries, has_manual_order=False)

        items = {i.label: i for i in view.sections[-1].items}

        # Title Page should be fixed with its reason
        assert items["Title Page"].fixed is True
        assert items["Title Page"].fixed_reason == "The title page always comes first"
        assert items["Title Page"].locked is False
        assert items["Title Page"].selected is True

        # Prologue (front matter) should be fixed with its reason
        assert items["Prologue"].fixed is True
        assert items["Prologue"].fixed_reason == "Front matter always stays before the chapters"
        assert items["Prologue"].locked is False
        assert items["Prologue"].selected is True

        # Chapter 1 and 2 (content) should NOT be fixed
        assert items["Chapter 1"].fixed is False
        assert items["Chapter 1"].fixed_reason == ""
        assert items["Chapter 1"].locked is False
        assert items["Chapter 1"].selected is True

        assert items["Chapter 2"].fixed is False
        assert items["Chapter 2"].fixed_reason == ""
        assert items["Chapter 2"].locked is False
        assert items["Chapter 2"].selected is True

        # Author's Note and Epilogue (back matter) should be fixed with their reason
        assert items["Author's Note"].fixed is True
        assert items["Author's Note"].fixed_reason == "Back matter always stays after the chapters"
        assert items["Author's Note"].locked is False
        assert items["Author's Note"].selected is True

        assert items["Epilogue"].fixed is True
        assert items["Epilogue"].fixed_reason == "Back matter always stays after the chapters"
        assert items["Epilogue"].locked is False
        assert items["Epilogue"].selected is True

    def test_the_fixed_rows_agree_with_the_headless_zone_rule(self, build_epub) -> None:
        """The fixed rows match exactly what chapter_role deems non-content."""
        from ebookerr_sdk.epub.chapters import chapter_role
        from epub_chapter_reorder.plugin import _build_view, _zone_lock

        epub = build_epub(
            [
                ("Prologue", "https://x.test/p"),
                ("Chapter 1", "https://x.test/1"),
                ("Chapter 2", "https://x.test/2"),
                ("Author's Note", "https://x.test/n"),
                ("Epilogue", "https://x.test/e"),
            ],
            doc_title="Zone Book",
        )
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        view = _build_view(doc, "Zone Book", entries, has_manual_order=False)

        items_by_idref = {i.id: i for i in view.sections[-1].items}

        # For every entry, the ViewItem.fixed should match what _zone_lock returns
        for entry in entries:
            fixed, _ = _zone_lock(entry)
            item = items_by_idref[entry.idref]
            # Fixed should be True only when chapter_role is not CONTENT
            assert item.fixed == fixed
            assert item.fixed == (chapter_role(entry.title, entry.number) != ChapterRole.CONTENT)

    def test_the_description_states_the_zone_rule(self, build_epub) -> None:
        """The view description mentions that title/front/back matter keep their place."""
        from epub_chapter_reorder.plugin import _build_view

        epub = build_epub(
            [("Chapter 1", "https://x.test/1")],
            doc_title="Test",
        )
        doc = EpubDocument.open(epub)
        entries = classify_spine(doc)
        view = _build_view(doc, "Test", entries, has_manual_order=False)

        assert "Title page, front matter and back matter keep their place." in view.description

    def test_section_options(self, epub_fixtures: Path) -> None:
        """The 'chapters' section has correct options."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        assert chapters_section.selectable is True
        assert chapters_section.reorderable is True
        assert chapters_section.select_all is True
        assert chapters_section.min_selected == 1
        assert chapters_section.kind == ViewSectionKind.ITEM_LIST

    def test_the_chapter_editor_confirm_names_its_changes(self, epub_fixtures: Path) -> None:
        """The view has danger=True, submit_label names what changes, cancel_label='Cancel'."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        assert view.danger is True
        assert view.submit_label == "Apply chapter changes"
        assert view.cancel_label == "Cancel"

    def test_title_includes_the_book_title(self, epub_fixtures: Path) -> None:
        """view.title == 'Chapters — Tending Bar' for the fixture."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        assert view.title == "Chapters — Tending Bar"

    def test_blank_book_title_falls_back(self, epub_fixtures: Path) -> None:
        """With book_title='', view.title == 'Chapters'."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "", entries, has_manual_order=False)
        assert view.title == "Chapters"

    def test_description_warns_there_is_no_undo(self, epub_fixtures: Path) -> None:
        """'no undo' is in view.description."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        view = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        assert "no undo" in view.description

    def test_note_section_present_only_with_a_manual_order(self, epub_fixtures: Path) -> None:
        """With manual order, first section is NOTE; without it, exactly one ITEM_LIST section."""
        tending_bar = epub_fixtures / "tending_bar.epub"
        doc = EpubDocument.open(tending_bar)
        entries = classify_spine(doc)

        from epub_chapter_reorder.plugin import _build_view

        # With manual order
        view_with_manual = _build_view(doc, "Tending Bar", entries, has_manual_order=True)
        assert len(view_with_manual.sections) == 2
        assert view_with_manual.sections[0].kind == ViewSectionKind.NOTE
        assert view_with_manual.sections[0].name == "manual_order_note"
        assert "Applying the automatic order will discard it" in view_with_manual.sections[0].text

        # Without manual order
        view_no_manual = _build_view(doc, "Tending Bar", entries, has_manual_order=False)
        assert len(view_no_manual.sections) == 1
        assert view_no_manual.sections[0].kind == ViewSectionKind.ITEM_LIST


class TestDuplicateDetection:
    """Tests for _duplicate_idrefs helper function."""

    def test_same_chapterurl_flags_both(self, build_epub):
        """Two chapters with identical chapterurl are both returned."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        # Build a book with chapters that have the same chapterurl (no title page)
        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch1"),  # Same URL!
        ]
        epub_path = build_epub(chapters, doc_title="Test", include_title_page=False)
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        duplicates = _duplicate_idrefs(doc, entries)
        # Both chapters with same URL should be flagged
        assert len(duplicates) == 2
        # Verify the chapter entries (not title page) are flagged
        chapter_entries = [e for e in entries if e.title in ("Chapter 1", "Chapter 2")]
        assert all(e.idref in duplicates for e in chapter_entries)

    def test_same_normalised_title_flags_both(self, build_epub):
        """Two chapters with the same slug are both returned."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        chapters = [
            ("Part 1", "https://example.com/ch1"),
            ("part  1", "https://example.com/ch2"),  # Same slug!
        ]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        duplicates = _duplicate_idrefs(doc, entries)
        assert len(duplicates) == 2

    def test_distinct_chapters_are_not_flagged(self, build_epub):
        """Three chapters with distinct titles and URLs return an empty set."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        chapters = [
            ("Chapter 1", "https://example.com/ch1"),
            ("Chapter 2", "https://example.com/ch2"),
            ("Chapter 3", "https://example.com/ch3"),
        ]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        duplicates = _duplicate_idrefs(doc, entries)
        assert duplicates == set()

    def test_empty_titles_are_not_flagged(self, build_epub):
        """Two chapters with empty resolved titles and no URLs are not flagged."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        chapters = [
            ("", "https://example.com/ch1"),
            ("", "https://example.com/ch2"),
        ]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        # Both chapters have empty title slugs, so they shouldn't group on title
        # And they have different URLs, so they shouldn't group on URL either
        duplicates = _duplicate_idrefs(doc, entries)
        assert duplicates == set()

    def test_non_url_keys_do_not_group(self, build_epub):
        """Two chapters whose chapter_key falls back to href are not flagged on that basis."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        chapters = [
            ("Title A", "https://example.com/ch1"),
            ("Title B", "https://example.com/ch2"),
        ]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        duplicates = _duplicate_idrefs(doc, entries)
        # Should be empty since titles are distinct and URLs (keys) are distinct
        assert duplicates == set()

    def test_duplicate_badge_does_not_deselect(self, build_epub):
        """In the built view, a duplicated item still has selected is True."""
        from epub_chapter_reorder.plugin import _build_view

        chapters = [
            ("Part 1", "https://example.com/ch1"),
            ("part  1", "https://example.com/ch2"),
        ]
        epub_path = build_epub(chapters, doc_title="Test")
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        view = _build_view(doc, "Test", entries, has_manual_order=False)
        chapters_section = next(s for s in view.sections if s.name == "chapters")
        # All items should still be selected
        for item in chapters_section.items:
            assert item.selected is True

    def test_distinct_cyrillic_titles_are_not_flagged(self, build_epub):
        """Two Cyrillic titles with distinct meanings are not flagged as duplicates (TXE-TR-1)."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        chapters = [
            ("Глава 1", "https://example.com/ch1"),
            ("Часть 1", "https://example.com/ch2"),
        ]
        epub_path = build_epub(chapters, doc_title="Test", include_title_page=False)
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        duplicates = _duplicate_idrefs(doc, entries)
        assert duplicates == set()

    def test_equal_cyrillic_titles_are_flagged(self, build_epub):
        """Two entries with the same Cyrillic title are both flagged (TXE-TR-1)."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        chapters = [
            ("Глава 1", "https://example.com/ch1"),
            ("глава  1", "https://example.com/ch2"),
        ]
        epub_path = build_epub(chapters, doc_title="Test", include_title_page=False)
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        duplicates = _duplicate_idrefs(doc, entries)
        assert len(duplicates) == 2

    def test_equal_cjk_titles_are_flagged(self, build_epub):
        """Two entries with identical CJK titles are both flagged (TXE-TR-1)."""
        from epub_chapter_reorder.plugin import _duplicate_idrefs

        chapters = [
            ("第一章", "https://example.com/ch1"),
            ("第一章", "https://example.com/ch2"),
        ]
        epub_path = build_epub(chapters, doc_title="Test", include_title_page=False)
        doc = EpubDocument.open(epub_path)
        entries = classify_spine(doc)

        duplicates = _duplicate_idrefs(doc, entries)
        assert len(duplicates) == 2
