"""Tests for EPUB merge chapter planning (``MR-FLAT-1``, ``MR-RENAME-1``)."""

import logging

import pytest
from epub_merge.merge.chapters import plan_chapters
from epub_merge.merge.model import InputBook, InputChapter


def _book(index: int, *labels: str) -> InputBook:
    """Build a neutral InputBook with the given chapter labels.

    Args:
        index: Book index.
        *labels: Chapter labels in reading order.

    Returns:
        An InputBook with neutral metadata and built InputChapter values.
    """
    chapters = tuple(
        InputChapter(
            label=label,
            href=f"OEBPS/file{i:04d}.xhtml",
            item_id=f"file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=b"<html/>",
        )
        for i, label in enumerate(labels)
    )
    return InputBook(
        index=index,
        name=f"book{index}.epub",
        version="2.0",
        title=f"Book {index}",
        creators=(),
        contributors=(),
        language="en",
        identifier=f"id-{index}",
        source=None,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root="OEBPS",
        chapters=chapters,
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )


def test_preserves_book_and_chapter_order() -> None:
    """Books and chapters maintain input order in the plan."""
    books = [_book(0, "A1", "A2"), _book(1, "B1")]
    planned = plan_chapters(books)

    assert [p.label for p in planned] == ["A1", "A2", "B1"]
    assert [p.book_index for p in planned] == [0, 0, 1]


def test_renames_to_semantic_filenames() -> None:
    """Chapter labels are transformed into semantic, numbered filenames."""
    books = [
        _book(0, "Title Page", "Three Square Meals - Chapter 174"),
        _book(1, "Three Square Meals - Chapter 180"),
    ]
    planned = plan_chapters(books)

    filenames = [p.filename for p in planned]
    assert filenames == [
        "title_page.xhtml",
        "chapter_174_three_square_meals.xhtml",
        "chapter_180_three_square_meals.xhtml",
    ]


def test_item_id_is_filename_stem() -> None:
    """Every PlannedChapter's item_id is the filename without .xhtml extension."""
    books = [_book(0, "Chapter 5", "Chapter 10")]
    planned = plan_chapters(books)

    for p in planned:
        expected_stem = p.filename[: -len(".xhtml")]
        assert p.item_id == expected_stem


def test_no_output_filename_contains_a_slash() -> None:
    """No planned filename contains a forward slash (MR-FLAT-1)."""
    books = [_book(0, "Title Page", "Chapter 1", "Chapter 2")]
    planned = plan_chapters(books)

    for p in planned:
        assert "/" not in p.filename


def test_no_output_name_contains_merged() -> None:
    """No planned filename or item_id contains 'merged' (MR-CLEAN-1)."""
    books = [_book(0, "Chapter 1", "Chapter 2")]
    planned = plan_chapters(books)

    for p in planned:
        assert "merged" not in p.filename.lower()
        assert "merged" not in p.item_id.lower()


def test_original_filenames_do_not_survive() -> None:
    """Input filenames do not appear in planned filenames (SH-3)."""
    books = [
        InputBook(
            index=0,
            name="book0.epub",
            version="2.0",
            title="Book 0",
            creators=(),
            contributors=(),
            language="en",
            identifier="id-0",
            source=None,
            rights=None,
            publisher=None,
            subjects=(),
            dates=(),
            page_direction="ltr",
            content_root="OEBPS",
            chapters=(
                InputChapter(
                    label="Chapter A",
                    href="OEBPS/file0001.xhtml",
                    item_id="file0001",
                    media_type="application/xhtml+xml",
                    xhtml=b"<html/>",
                ),
                InputChapter(
                    label="Chapter B",
                    href="OEBPS/chapter_001.xhtml",
                    item_id="chapter_001",
                    media_type="application/xhtml+xml",
                    xhtml=b"<html/>",
                ),
            ),
            resources=(),
            stylesheets=(),
            cover_href=None,
            cover_media_type=None,
        ),
    ]
    planned = plan_chapters(books)

    input_filenames = {"file0001.xhtml", "chapter_001.xhtml"}
    for p in planned:
        assert p.filename not in input_filenames


def test_duplicate_labels_across_books_get_suffix() -> None:
    """Duplicate labels across books are renamed with _b, _c, ... suffixes."""
    books = [_book(0, "Book - Chapter 5"), _book(1, "Book - Chapter 5")]
    planned = plan_chapters(books)

    filenames = [p.filename for p in planned]
    assert len(filenames) == 2
    assert filenames[0] == "chapter_05_book.xhtml"
    assert filenames[1] == "chapter_05_book_b.xhtml"


def test_numbers_are_carried() -> None:
    """Chapter numbers are extracted and carried to PlannedChapter."""
    books = [
        _book(0, "Chapter 174", "Title Page"),
    ]
    planned = plan_chapters(books)

    assert planned[0].number == 174
    assert planned[1].number is None


def test_source_href_is_preserved() -> None:
    """The source href from InputChapter is preserved in PlannedChapter."""
    books = [_book(0, "Chapter 1")]
    planned = plan_chapters(books)

    assert planned[0].source_href == "OEBPS/file0000.xhtml"


def test_xhtml_bytes_are_carried_unchanged() -> None:
    """XHTML bytes from InputChapter are carried unchanged to PlannedChapter."""
    xhtml_content = b"<html><body>Test</body></html>"
    book = InputBook(
        index=0,
        name="book0.epub",
        version="2.0",
        title="Book 0",
        creators=(),
        contributors=(),
        language="en",
        identifier="id-0",
        source=None,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root="OEBPS",
        chapters=(
            InputChapter(
                label="Chapter 1",
                href="OEBPS/file0000.xhtml",
                item_id="file0000",
                media_type="application/xhtml+xml",
                xhtml=xhtml_content,
            ),
        ),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )
    planned = plan_chapters([book])

    assert planned[0].xhtml == xhtml_content


def test_empty_books_produce_empty_plan() -> None:
    """Empty book list produces empty plan; books with no chapters contribute nothing."""
    assert plan_chapters([]) == []

    books = [_book(0), _book(1)]  # Books with no chapters
    assert plan_chapters(books) == []


def test_logs_info_summary(caplog: pytest.LogCaptureFixture) -> None:
    """Planning chapters emits an INFO log with the summary."""
    books = [_book(0, "A", "B"), _book(1, "C")]
    with caplog.at_level(logging.INFO):
        plan_chapters(books)

    assert any(
        "Merge planned 3 chapter(s) from 2 book(s)" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


def test_logs_debug_per_chapter(caplog: pytest.LogCaptureFixture) -> None:
    """One DEBUG log per chapter is emitted with position and filename."""
    books = [_book(0, "A", "B"), _book(1, "C")]
    with caplog.at_level(logging.DEBUG):
        plan_chapters(books)

    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(debug_records) == 3

    for record in debug_records:
        assert "-> " in record.message
        # Check that the filename is mentioned
        assert any(p.filename in record.message for book in books for p in plan_chapters([book]))
