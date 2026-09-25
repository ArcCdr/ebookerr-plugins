"""Tests for epub_merge.merge.chapter_urls — stamping chapter URLs onto merged chapters."""

from __future__ import annotations

import logging

from ebookerr_sdk.epub.xhtml import ChapterDocument
from epub_merge.merge.chapter_urls import stamp_chapter_urls
from epub_merge.merge.model import InputBook, InputChapter, PlannedChapter


def _make_chapter_xhtml(chapter_url: str | None = None) -> str:
    """Helper to create chapter XHTML with optional chapterurl meta."""
    meta_tag = ""
    if chapter_url is not None:
        meta_tag = f'<meta name="chapterurl" content="{chapter_url}"/>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>Chapter</title>
{meta_tag}
</head>
<body><p>Content</p></body>
</html>"""


def _make_planned_chapter(
    label: str,
    filename: str,
    item_id: str,
    source_href: str,
    chapter_url: str | None = None,
    book_index: int = 0,
    number: int | None = None,
) -> PlannedChapter:
    """Helper to create a PlannedChapter with optional chapter URL in XHTML."""
    xhtml = _make_chapter_xhtml(chapter_url)
    return PlannedChapter(
        label=label,
        filename=filename,
        item_id=item_id,
        number=number,
        book_index=book_index,
        source_href=source_href,
        xhtml=xhtml.encode("utf-8"),
    )


def _make_input_chapter(
    label: str,
    href: str,
    item_id: str,
    xhtml_str: str | None = None,
) -> InputChapter:
    """Helper to create an InputChapter."""
    if xhtml_str is None:
        xhtml_str = _make_chapter_xhtml()
    return InputChapter(
        label=label,
        href=href,
        item_id=item_id,
        media_type="application/xhtml+xml",
        xhtml=xhtml_str.encode("utf-8"),
    )


def _make_input_book(
    index: int,
    title: str,
    chapters: list[InputChapter],
    source: str | None = None,
    title_page: InputChapter | None = None,
) -> InputBook:
    """Helper to create an InputBook."""
    return InputBook(
        index=index,
        name=f"book_{index}.epub",
        version="3.0",
        title=title,
        creators=(),
        contributors=(),
        language="en",
        identifier=f"uuid:{index}",
        source=source,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root="OEBPS",
        chapters=tuple(chapters),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
        title_page=title_page,
    )


def _read_chapter_url(chapter: PlannedChapter) -> str | None:
    """Helper to read the chapterurl meta from a PlannedChapter."""
    doc = ChapterDocument.parse(chapter.xhtml.decode("utf-8"))
    return doc.meta("chapterurl")


class TestStampChapterUrls:
    """Test stamp_chapter_urls behavior on various scenarios."""

    def test_single_chapter_book_gets_book_url(self) -> None:
        """One book, one chapter with no meta, book_urls specified → chapter declares book URL."""
        chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=0,
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book = _make_input_book(0, "Test Book", [input_ch])

        result = stamp_chapter_urls([chapter], [book], ["https://book/one"])

        assert len(result) == 1
        assert _read_chapter_url(result[0]) == "https://book/one"

    def test_single_chapter_book_book_url_overrides_existing(self) -> None:
        """Single chapter book: book URL wins over existing chapter URL."""
        chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            source_href="OEBPS/ch1.xhtml",
            chapter_url="https://old/ch",
            book_index=0,
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book = _make_input_book(0, "Test Book", [input_ch])

        result = stamp_chapter_urls([chapter], [book], ["https://book/one"])

        assert len(result) == 1
        assert _read_chapter_url(result[0]) == "https://book/one"

    def test_single_chapter_book_falls_back_to_chapter_url(self) -> None:
        """Single chapter with existing URL and no book URL → unchanged."""
        xhtml = _make_chapter_xhtml("https://old/ch")
        chapter = PlannedChapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            number=None,
            book_index=0,
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml.encode("utf-8"),
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book = _make_input_book(0, "Test Book", [input_ch])

        result = stamp_chapter_urls([chapter], [book], [])

        assert len(result) == 1
        # Should be unchanged: identity check on bytes
        assert result[0].xhtml == chapter.xhtml
        assert _read_chapter_url(result[0]) == "https://old/ch"

    def test_multi_chapter_all_missing_gets_book_url(self) -> None:
        """Multi-chapter book with none declaring URL → all get book URL."""
        chapters = [
            _make_planned_chapter(
                label="Chapter 1",
                filename="chapter_001.xhtml",
                item_id="c001",
                source_href="OEBPS/ch1.xhtml",
                chapter_url=None,
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 2",
                filename="chapter_002.xhtml",
                item_id="c002",
                source_href="OEBPS/ch2.xhtml",
                chapter_url=None,
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 3",
                filename="chapter_003.xhtml",
                item_id="c003",
                source_href="OEBPS/ch3.xhtml",
                chapter_url=None,
                book_index=0,
            ),
        ]
        input_chs = [
            _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001"),
            _make_input_chapter("Chapter 2", "OEBPS/ch2.xhtml", "c002"),
            _make_input_chapter("Chapter 3", "OEBPS/ch3.xhtml", "c003"),
        ]
        book = _make_input_book(0, "Test Book", input_chs)

        result = stamp_chapter_urls(chapters, [book], ["https://book/multi"])

        assert len(result) == 3
        for chapter in result:
            assert _read_chapter_url(chapter) == "https://book/multi"

    def test_multi_chapter_partial_left_untouched(self) -> None:
        """Multi-chapter with some URLs: all chapters left untouched, bytes unchanged."""
        chapters = [
            _make_planned_chapter(
                label="Chapter 1",
                filename="chapter_001.xhtml",
                item_id="c001",
                source_href="OEBPS/ch1.xhtml",
                chapter_url=None,
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 2",
                filename="chapter_002.xhtml",
                item_id="c002",
                source_href="OEBPS/ch2.xhtml",
                chapter_url="https://x/2",
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 3",
                filename="chapter_003.xhtml",
                item_id="c003",
                source_href="OEBPS/ch3.xhtml",
                chapter_url=None,
                book_index=0,
            ),
        ]
        input_chs = [
            _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001"),
            _make_input_chapter("Chapter 2", "OEBPS/ch2.xhtml", "c002"),
            _make_input_chapter("Chapter 3", "OEBPS/ch3.xhtml", "c003"),
        ]
        book = _make_input_book(0, "Test Book", input_chs)

        result = stamp_chapter_urls(chapters, [book], ["https://book/multi"])

        assert len(result) == 3
        # Chapter 1 and 3 should be unchanged (bytes identical)
        assert result[0].xhtml == chapters[0].xhtml
        assert result[2].xhtml == chapters[2].xhtml
        # Chapter 2 should remain with its URL
        assert _read_chapter_url(result[1]) == "https://x/2"

    def test_multi_chapter_all_present_left_untouched(self) -> None:
        """Multi-chapter with all declaring URLs → all bytes unchanged."""
        chapters = [
            _make_planned_chapter(
                label="Chapter 1",
                filename="chapter_001.xhtml",
                item_id="c001",
                source_href="OEBPS/ch1.xhtml",
                chapter_url="https://x/1",
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 2",
                filename="chapter_002.xhtml",
                item_id="c002",
                source_href="OEBPS/ch2.xhtml",
                chapter_url="https://x/2",
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 3",
                filename="chapter_003.xhtml",
                item_id="c003",
                source_href="OEBPS/ch3.xhtml",
                chapter_url="https://x/3",
                book_index=0,
            ),
        ]
        input_chs = [
            _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001"),
            _make_input_chapter("Chapter 2", "OEBPS/ch2.xhtml", "c002"),
            _make_input_chapter("Chapter 3", "OEBPS/ch3.xhtml", "c003"),
        ]
        book = _make_input_book(0, "Test Book", input_chs)

        result = stamp_chapter_urls(chapters, [book], ["https://book/multi"])

        assert len(result) == 3
        for i, chapter in enumerate(result):
            # All should be unchanged
            assert chapter.xhtml == chapters[i].xhtml

    def test_book_url_falls_back_to_dc_source(self) -> None:
        """No caller URL, but book.source is https → chapter gets book.source."""
        chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=0,
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book = _make_input_book(0, "Test Book", [input_ch], source="https://epub/src")

        result = stamp_chapter_urls([chapter], [book], [])

        assert len(result) == 1
        assert _read_chapter_url(result[0]) == "https://epub/src"

    def test_book_url_ignores_non_http_source(self) -> None:
        """Non-http source (e.g. urn:uuid) is ignored → chapter unchanged."""
        xhtml = _make_chapter_xhtml()
        chapter = PlannedChapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            number=None,
            book_index=0,
            source_href="OEBPS/ch1.xhtml",
            xhtml=xhtml.encode("utf-8"),
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book = _make_input_book(0, "Test Book", [input_ch], source="urn:uuid:abc")

        result = stamp_chapter_urls([chapter], [book], [])

        assert len(result) == 1
        # Should be unchanged
        assert result[0].xhtml == chapter.xhtml

    def test_book_url_falls_back_to_title_page(self) -> None:
        """No caller URL, no usable source → falls back to title_page.story_url()."""
        chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=0,
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        title_page_xhtml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            "<head><title>Title</title></head>\n"
            '<body><h3><a href="https://tp/story">Story</a></h3></body>\n'
            "</html>"
        )
        title_page = _make_input_chapter(
            "Title Page", "OEBPS/title_page.xhtml", "title_page", title_page_xhtml
        )
        book = _make_input_book(0, "Test Book", [input_ch], title_page=title_page)

        result = stamp_chapter_urls([chapter], [book], [])

        assert len(result) == 1
        assert _read_chapter_url(result[0]) == "https://tp/story"

    def test_caller_url_wins_over_dc_source(self) -> None:
        """Both caller URL and dc:source present → caller URL wins."""
        chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=0,
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book = _make_input_book(0, "Test Book", [input_ch], source="https://epub/src")

        result = stamp_chapter_urls([chapter], [book], ["https://caller/url"])

        assert len(result) == 1
        assert _read_chapter_url(result[0]) == "https://caller/url"

    def test_two_books_stamped_independently(self) -> None:
        """Two books: book 0 (one-chapter) gets URL A, book 1 (three-chapter) gets URL B."""
        # Book 0: single chapter
        ch0_0 = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c0_1",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=0,
        )
        # Book 1: three chapters
        ch1_0 = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_002.xhtml",
            item_id="c1_1",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=1,
        )
        ch1_1 = _make_planned_chapter(
            label="Chapter 2",
            filename="chapter_003.xhtml",
            item_id="c1_2",
            source_href="OEBPS/ch2.xhtml",
            chapter_url=None,
            book_index=1,
        )
        ch1_2 = _make_planned_chapter(
            label="Chapter 3",
            filename="chapter_004.xhtml",
            item_id="c1_3",
            source_href="OEBPS/ch3.xhtml",
            chapter_url=None,
            book_index=1,
        )

        input_ch0_0 = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c0_1")
        book0 = _make_input_book(0, "Book 0", [input_ch0_0])

        input_ch1_0 = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c1_1")
        input_ch1_1 = _make_input_chapter("Chapter 2", "OEBPS/ch2.xhtml", "c1_2")
        input_ch1_2 = _make_input_chapter("Chapter 3", "OEBPS/ch3.xhtml", "c1_3")
        book1 = _make_input_book(1, "Book 1", [input_ch1_0, input_ch1_1, input_ch1_2])

        result = stamp_chapter_urls(
            [ch0_0, ch1_0, ch1_1, ch1_2],
            [book0, book1],
            ["https://book-a", "https://book-b"],
        )

        assert len(result) == 4
        assert _read_chapter_url(result[0]) == "https://book-a"
        assert _read_chapter_url(result[1]) == "https://book-b"
        assert _read_chapter_url(result[2]) == "https://book-b"
        assert _read_chapter_url(result[3]) == "https://book-b"

    def test_chapters_returned_in_original_order(self) -> None:
        """Returned list's filename sequence equals input's."""
        chapters = [
            _make_planned_chapter(
                label="Chapter 1",
                filename="chapter_001.xhtml",
                item_id="c001",
                source_href="OEBPS/ch1.xhtml",
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 2",
                filename="chapter_002.xhtml",
                item_id="c002",
                source_href="OEBPS/ch2.xhtml",
                book_index=0,
            ),
        ]
        input_chs = [
            _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001"),
            _make_input_chapter("Chapter 2", "OEBPS/ch2.xhtml", "c002"),
        ]
        book = _make_input_book(0, "Test Book", input_chs)

        result = stamp_chapter_urls(chapters, [book], ["https://book/url"])

        filenames = [c.filename for c in result]
        expected = [c.filename for c in chapters]
        assert filenames == expected

    def test_unparseable_chapter_left_untouched(self) -> None:
        """Chapter with malformed XHTML is returned byte-identical."""
        xhtml_bad = b"not xml"
        chapter = PlannedChapter(
            label="Bad Chapter",
            filename="bad.xhtml",
            item_id="bad",
            number=None,
            book_index=0,
            source_href="OEBPS/bad.xhtml",
            xhtml=xhtml_bad,
        )
        input_ch = _make_input_chapter("Bad Chapter", "OEBPS/bad.xhtml", "bad")
        book = _make_input_book(0, "Test Book", [input_ch])

        result = stamp_chapter_urls([chapter], [book], ["https://book/url"])

        assert len(result) == 1
        assert result[0].xhtml == xhtml_bad

    def test_idempotent_second_pass_changes_nothing(self) -> None:
        """Running stamp_chapter_urls twice returns byte-identical xhtml."""
        chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=0,
        )
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book = _make_input_book(0, "Test Book", [input_ch])

        result1 = stamp_chapter_urls([chapter], [book], ["https://book/url"])
        result2 = stamp_chapter_urls(result1, [book], ["https://book/url"])

        assert len(result2) == 1
        assert result2[0].xhtml == result1[0].xhtml

    def test_logs_per_book_and_total(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """Logs DEBUG per book and INFO overall."""
        chapters = [
            _make_planned_chapter(
                label="Chapter 1",
                filename="chapter_001.xhtml",
                item_id="c001",
                source_href="OEBPS/ch1.xhtml",
                chapter_url=None,
                book_index=0,
            ),
            _make_planned_chapter(
                label="Chapter 1",
                filename="chapter_002.xhtml",
                item_id="c1_1",
                source_href="OEBPS/ch1.xhtml",
                chapter_url=None,
                book_index=1,
            ),
            _make_planned_chapter(
                label="Chapter 2",
                filename="chapter_003.xhtml",
                item_id="c1_2",
                source_href="OEBPS/ch2.xhtml",
                chapter_url=None,
                book_index=1,
            ),
            _make_planned_chapter(
                label="Chapter 3",
                filename="chapter_004.xhtml",
                item_id="c1_3",
                source_href="OEBPS/ch3.xhtml",
                chapter_url=None,
                book_index=1,
            ),
        ]
        input_ch0_0 = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book0 = _make_input_book(0, "Book 0", [input_ch0_0])

        input_ch1_0 = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c1_1")
        input_ch1_1 = _make_input_chapter("Chapter 2", "OEBPS/ch2.xhtml", "c1_2")
        input_ch1_2 = _make_input_chapter("Chapter 3", "OEBPS/ch3.xhtml", "c1_3")
        book1 = _make_input_book(1, "Book 1", [input_ch1_0, input_ch1_1, input_ch1_2])

        with caplog.at_level(logging.DEBUG):
            stamp_chapter_urls(
                chapters,
                [book0, book1],
                ["https://book-a", "https://book-b"],
            )

        # Check for DEBUG record per book
        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert any(
            "Merge stamped 1 chapter URL(s) from input 0" in r.message for r in debug_records
        )
        assert any(
            "Merge stamped 3 chapter URL(s) from input 1" in r.message for r in debug_records
        )

        # Check for INFO record total
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert any(
            "Merge stamped chapter URLs on 4 of 4 chapter(s)" in r.message for r in info_records
        )

    def test_empty_input_returns_empty(self) -> None:
        """stamp_chapter_urls([], [], []) returns []."""
        result = stamp_chapter_urls([], [], [])
        assert result == []

    def test_book_with_no_chapters_skipped(self) -> None:
        """An InputBook contributing no planned chapters does not affect others."""
        chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="chapter_001.xhtml",
            item_id="c001",
            source_href="OEBPS/ch1.xhtml",
            chapter_url=None,
            book_index=0,
        )
        # Book 0 has one chapter
        input_ch0 = _make_input_chapter("Chapter 1", "OEBPS/ch1.xhtml", "c001")
        book0 = _make_input_book(0, "Book 0", [input_ch0])

        # Book 1 has no chapters in the planned output (book_index=1 doesn't appear)
        book1 = _make_input_book(1, "Book 1", [])

        result = stamp_chapter_urls([chapter], [book0, book1], ["https://a", "https://b"])

        assert len(result) == 1
        assert _read_chapter_url(result[0]) == "https://a"

    def test_single_chapter_rule_ignores_the_title_page_by_id(self) -> None:
        """The one-chapter rule excludes title pages by manifest id, not filename.

        A book with a title page (identified by manifest id) plus one chapter with
        filename cover_story.xhtml should apply the single-chapter rule to the
        actual content chapter, stamping the book URL even though there's technically
        a title page entry in the spine.
        """
        title_page = _make_planned_chapter(
            label="Title Page",
            filename="title_page.xhtml",
            item_id="title_page",  # Recognized as title page by id
            source_href="OEBPS/title_page.xhtml",
            chapter_url=None,
            book_index=0,
        )
        content_chapter = _make_planned_chapter(
            label="Chapter 1",
            filename="cover_story.xhtml",
            item_id="ch001",
            source_href="OEBPS/cover_story.xhtml",
            chapter_url=None,
            book_index=0,
        )
        # Input chapters (used to build the InputBook)
        input_tp = _make_input_chapter("Title Page", "OEBPS/title_page.xhtml", "title_page")
        input_ch = _make_input_chapter("Chapter 1", "OEBPS/cover_story.xhtml", "ch001")
        book = _make_input_book(0, "Book 0", [input_tp, input_ch])

        result = stamp_chapter_urls(
            [title_page, content_chapter],
            [book],
            ["https://book/url"],
        )

        assert len(result) == 2
        # Title page should remain unchanged (no stamping)
        assert result[0].xhtml == title_page.xhtml
        # Content chapter should be stamped (one-chapter rule applied, not including title page)
        assert _read_chapter_url(result[1]) == "https://book/url"
