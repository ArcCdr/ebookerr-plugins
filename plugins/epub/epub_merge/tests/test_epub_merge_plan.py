"""Tests for assembling the EPUB merge plan (manifest, spine, TOC, metadata)."""

from __future__ import annotations

import logging
import re

import pytest
from epub_merge.merge.chapters import plan_chapters
from epub_merge.merge.model import InputBook, InputChapter, InputResource, MergeOptions
from epub_merge.merge.plan import build_merge_plan

_NCNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._\-]*$")
_UUID_URN_RE = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _make_proper_xhtml(chapter_url: str | None = None) -> str:
    """Create valid XHTML with optional chapterurl meta for stamping tests."""
    meta_tag = ""
    if chapter_url is not None:
        meta_tag = f'<meta name="chapterurl" content="{chapter_url}"/>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter</title>
{meta_tag}
</head>
<body><p>Content</p></body>
</html>"""


def _book(
    index: int,
    *labels: str,
    resources: tuple[InputResource, ...] = (),
    title: str | None = None,
    language: str = "en",
    creators: tuple[tuple[str, str | None], ...] = (),
    contributors: tuple[tuple[str, str], ...] = (),
    subjects: tuple[str, ...] = (),
    dates: tuple[tuple[str, str], ...] = (),
    page_direction: str = "ltr",
    content_root: str = "OEBPS",
    cover_href: str | None = None,
    cover_media_type: str | None = None,
) -> InputBook:
    """Build a neutral InputBook with the given chapter labels and resources.

    Args:
        index: Book index.
        *labels: Chapter labels in reading order.
        resources: Non-chapter resources to include.
        title: Book title; defaults to ``f"Book {index}"``.
        language: Language code.
        creators: Tuple of (name, file_as) tuples.
        contributors: Tuple of (name, role) tuples.
        subjects: Tuple of subject strings.
        dates: Tuple of (date_string, event) tuples.
        page_direction: Page progression direction ("ltr", "rtl", or "").
        content_root: The book's content directory.
        cover_href: Optional cover image href.
        cover_media_type: Optional cover image media type.

    Returns:
        An InputBook with neutral metadata and the provided chapters/resources.
    """
    chapters = tuple(
        InputChapter(
            label=label,
            href=f"{content_root}/file{i:04d}.xhtml",
            item_id=f"file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=f"<html>{label}</html>".encode(),
        )
        for i, label in enumerate(labels)
    )
    return InputBook(
        index=index,
        name=f"book{index}.epub",
        version="2.0",
        title=title if title is not None else f"Book {index}",
        creators=creators,
        contributors=contributors,
        language=language,
        identifier=f"id-{index}",
        source=None,
        rights=None,
        publisher=None,
        subjects=subjects,
        dates=dates,
        page_direction=page_direction,
        content_root=content_root,
        chapters=chapters,
        resources=resources,
        stylesheets=tuple(r.href for r in resources if r.media_type == "text/css"),
        cover_href=cover_href,
        cover_media_type=cover_media_type,
    )


def test_plan_has_one_manifest_entry_per_file_plus_ncx() -> None:
    """Manifest has one entry per chapter and resource, plus the NCX."""
    resource = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body{}")
    books = [
        _book(0, "Title Page", "Chapter 1", resources=(resource,)),
        _book(1, "Chapter 2"),
    ]
    plan = build_merge_plan(books, MergeOptions())

    assert len(plan.package.manifest) == 1 + 3 + 1
    assert plan.package.manifest[0].item_id == "ncx"
    assert plan.package.manifest[0].href == "toc.ncx"


def test_manifest_hrefs_for_chapters_have_no_slash() -> None:
    """Every XHTML manifest entry's href has no slash (ACC-FLAT-1)."""
    books = [_book(0, "Title Page", "Chapter 1", "Chapter 2")]
    plan = build_merge_plan(books, MergeOptions())

    for entry in plan.package.manifest:
        if entry.media_type == "application/xhtml+xml":
            assert "/" not in entry.href


def test_resource_hrefs_keep_their_directory() -> None:
    """A resource planned in a subdirectory keeps that directory in its href."""
    resource = InputResource(
        href="OEBPS/images/divider.png", media_type="image/png", data=b"\x89PNG"
    )
    books = [_book(0, "Chapter 1", resources=(resource,))]
    plan = build_merge_plan(books, MergeOptions())

    hrefs = [entry.href for entry in plan.package.manifest]
    assert "images/divider.png" in hrefs


def test_spine_matches_chapter_order() -> None:
    """Spine idrefs match planned chapter item_ids in order."""
    books = [_book(0, "A", "B"), _book(1, "C")]
    plan = build_merge_plan(books, MergeOptions())
    chapters = plan_chapters(books)

    assert [e.idref for e in plan.package.spine] == [c.item_id for c in chapters]


def test_toc_matches_chapters() -> None:
    """TOC entries match planned chapters, flat (no children)."""
    books = [_book(0, "A", "B"), _book(1, "C")]
    plan = build_merge_plan(books, MergeOptions())
    chapters = plan_chapters(books)

    assert [(e.label, e.href) for e in plan.toc] == [(c.label, c.filename) for c in chapters]
    for entry in plan.toc:
        assert entry.children == ()


def test_no_duplicate_manifest_ids_or_hrefs() -> None:
    """Manifest item_ids and hrefs are all distinct (MR-DEDUP-1)."""
    resource = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body{}")
    books = [_book(0, "A", "B", resources=(resource,)), _book(1, "C")]
    plan = build_merge_plan(books, MergeOptions())

    ids = [e.item_id for e in plan.package.manifest]
    hrefs = [e.href for e in plan.package.manifest]
    assert len(ids) == len(set(ids))
    assert len(hrefs) == len(set(hrefs))


def test_no_duplicate_spine_idrefs() -> None:
    """Spine idrefs are all distinct."""
    books = [_book(0, "A", "B"), _book(1, "C")]
    plan = build_merge_plan(books, MergeOptions())

    idrefs = [e.idref for e in plan.package.spine]
    assert len(idrefs) == len(set(idrefs))


def test_manifest_ids_are_valid_ncnames() -> None:
    """Every manifest item_id is a valid XML NCName (ACC-ID-1)."""
    resource = InputResource(href="OEBPS/1image.png", media_type="image/png", data=b"\x89PNG")
    books = [_book(0, "A", "B", resources=(resource,)), _book(1, "C")]
    plan = build_merge_plan(books, MergeOptions())

    for entry in plan.package.manifest:
        assert _NCNAME_RE.match(entry.item_id), entry.item_id


def test_resource_id_from_digit_leading_path() -> None:
    """A resource path starting with a digit yields an id that is a valid NCName."""
    resource = InputResource(href="OEBPS/1image.png", media_type="image/png", data=b"\x89PNG")
    books = [_book(0, "A", resources=(resource,))]
    plan = build_merge_plan(books, MergeOptions())

    resource_entry = next(e for e in plan.package.manifest if e.href == "1image.png")
    assert not resource_entry.item_id[0].isdigit()


def test_resource_id_collision_is_suffixed() -> None:
    """Resource paths that slugify identically get distinct manifest ids."""
    resources = (
        InputResource(href="OEBPS/a/b.png", media_type="image/png", data=b"\x89PNG1"),
        InputResource(href="OEBPS/a_b.png", media_type="image/png", data=b"\x89PNG2"),
    )
    books = [_book(0, "A", resources=resources)]
    plan = build_merge_plan(books, MergeOptions())

    resource_entries = [e for e in plan.package.manifest if e.href in ("a/b.png", "a_b.png")]
    assert len(resource_entries) == 2
    ids = {e.item_id for e in resource_entries}
    assert len(ids) == 2


def test_identifier_is_a_fresh_urn_uuid() -> None:
    """The output identifier is a freshly generated urn:uuid, different each call."""
    books = [_book(0, "A")]
    plan1 = build_merge_plan(books, MergeOptions())
    plan2 = build_merge_plan(books, MergeOptions())

    assert _UUID_URN_RE.match(plan1.identifier)
    assert plan1.identifier != plan2.identifier


def test_title_comes_from_survivor() -> None:
    """The output title is the survivor's (books[0]) title."""
    books = [_book(0, "A", title="Survivor Title"), _book(1, "B", title="Other Title")]
    plan = build_merge_plan(books, MergeOptions())

    assert plan.title == "Survivor Title"


def test_language_prefers_option_then_survivor() -> None:
    """Language: option overrides survivor; survivor overrides "en" default."""
    books = [_book(0, "A", language="de")]

    plan_with_option = build_merge_plan(books, MergeOptions(language="fr"))
    assert plan_with_option.package.metadata.language == "fr"

    plan_without_option = build_merge_plan(books, MergeOptions())
    assert plan_without_option.package.metadata.language == "de"

    books_no_lang = [_book(0, "A", language="")]
    plan_default = build_merge_plan(books_no_lang, MergeOptions())
    assert plan_default.package.metadata.language == "en"


def test_creators_come_from_survivor() -> None:
    """Output creators include all creators from every book."""
    survivor_creators = (("Jane Author", "Author, Jane"),)
    other_creators = (("Other", None),)
    books = [_book(0, "A", creators=survivor_creators), _book(1, "B", creators=other_creators)]
    plan = build_merge_plan(books, MergeOptions())

    assert ("Jane Author", "Author, Jane") in plan.package.metadata.creators
    assert ("Other", None) in plan.package.metadata.creators


def test_files_contain_every_chapter_and_resource() -> None:
    """plan.files contains every chapter filename and resource path with correct bytes."""
    resource = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body{}")
    books = [_book(0, "A", "B", resources=(resource,)), _book(1, "C")]
    plan = build_merge_plan(books, MergeOptions())
    chapters = plan_chapters(books)

    expected_paths = {c.filename for c in chapters} | {"style.css"}
    assert set(plan.files) == expected_paths
    for chapter in chapters:
        assert plan.files[chapter.filename] == chapter.xhtml


def test_version_is_epub2_for_epub2_inputs() -> None:
    """The output version is 2.0 for this baseline (EPUB3 arrives later)."""
    books = [_book(0, "A")]
    plan = build_merge_plan(books, MergeOptions())

    assert plan.version == "2.0"
    assert plan.package.version == "2.0"


def test_no_merged_substring_anywhere() -> None:
    """No manifest id/href, spine idref, TOC href, or output path contains 'merged'."""
    resource = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body{}")
    books = [_book(0, "A", "B", resources=(resource,)), _book(1, "C")]
    plan = build_merge_plan(books, MergeOptions())

    for entry in plan.package.manifest:
        assert "merged" not in entry.item_id.lower()
        assert "merged" not in entry.href.lower()
    for entry in plan.package.spine:
        assert "merged" not in entry.idref.lower()
    for entry in plan.toc:
        assert "merged" not in entry.href.lower()
    for path in plan.files:
        assert "merged" not in path.lower()


def test_logs_info_summary(caplog: pytest.LogCaptureFixture) -> None:
    """Building the plan emits an INFO log with the chapter count."""
    books = [_book(0, "A", "B"), _book(1, "C")]
    with caplog.at_level(logging.INFO):
        build_merge_plan(books, MergeOptions())

    assert any(
        "Merge plan built" in record.message and "3 chapter" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


def test_plan_collects_contributors_from_every_book() -> None:
    """Contributors from all books are collected in the merge plan."""
    books = [
        _book(0, "A", contributors=(("Translator A", "trl"),)),
        _book(1, "B", contributors=(("Translator B", "trl"),)),
    ]
    plan = build_merge_plan(books, MergeOptions())

    assert ("Translator A", "trl") in plan.package.metadata.contributors
    assert ("Translator B", "trl") in plan.package.metadata.contributors


def test_plan_collects_subjects() -> None:
    """Subjects from all books are collected in the merge plan."""
    books = [
        _book(0, "A", subjects=("Erotica", "Mind Control")),
        _book(1, "B", subjects=("Mind Control", "In-Progress")),
    ]
    plan = build_merge_plan(books, MergeOptions())

    assert "Erotica" in plan.package.metadata.subjects
    assert "Mind Control" in plan.package.metadata.subjects
    assert "In-Progress" in plan.package.metadata.subjects


def test_plan_uses_survivor_dates() -> None:
    """The merge plan's dates match the survivor's dates."""
    survivor_dates = (("publication", "2026-01-01"), ("modified", "2026-07-28"))
    other_dates = (("publication", "2025-01-01"),)
    books = [
        _book(0, "A", dates=survivor_dates),
        _book(1, "B", dates=other_dates),
    ]
    plan = build_merge_plan(books, MergeOptions())

    assert plan.package.metadata.dates == survivor_dates


def test_plan_warns_on_language_mismatch(caplog: pytest.LogCaptureFixture) -> None:
    """Building a plan with mismatched input languages emits a WARNING."""
    books = [_book(0, "A", language="en"), _book(1, "B", language="fr")]
    with caplog.at_level(logging.WARNING):
        build_merge_plan(books, MergeOptions())

    assert any(
        "Merge language mismatch" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


def test_plan_sets_page_direction_when_shared() -> None:
    """Two books with page_direction='rtl' sets plan.package.page_progression_direction to 'rtl'."""
    books = [_book(0, "A", page_direction="rtl"), _book(1, "B", page_direction="rtl")]
    plan = build_merge_plan(books, MergeOptions())

    assert plan.package.page_progression_direction == "rtl"


def test_plan_omits_page_direction_on_conflict() -> None:
    """Conflicting page directions set plan.package.page_progression_direction to None."""
    books = [
        _book(0, "A", page_direction="ltr"),
        _book(1, "B", page_direction="rtl"),
    ]
    plan = build_merge_plan(books, MergeOptions())

    assert plan.package.page_progression_direction is None


def test_plan_cover_manifest_spine_and_guide() -> None:
    """With a survivor cover, the plan integrates cover manifest, spine, and guide (AT-COVER-1)."""
    cover_resource = InputResource(
        href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
    )
    books = [
        _book(
            0,
            "Title Page",
            "Chapter 1",
            resources=(cover_resource,),
            cover_href="OEBPS/cover.jpg",
            cover_media_type="image/jpeg",
        ),
        _book(1, "Chapter 2"),
    ]
    plan = build_merge_plan(books, MergeOptions())

    # Check manifest has cover entry with id="cover"
    cover_manifest = next((e for e in plan.package.manifest if e.item_id == "cover"), None)
    assert cover_manifest is not None
    assert cover_manifest.href == "cover.xhtml"
    assert cover_manifest.media_type == "application/xhtml+xml"

    # Check spine has cover as first entry
    assert len(plan.package.spine) > 0
    assert plan.package.spine[0].idref == "cover"

    # Check guide has one cover entry
    guide_covers = [e for e in plan.package.guide if e.type == "cover"]
    assert len(guide_covers) == 1
    assert guide_covers[0].href == "cover.xhtml"

    # Check metadata.cover_item_id is set
    assert plan.package.metadata.cover_item_id is not None


def test_plan_cover_reuses_existing_resource_file() -> None:
    """The cover image appears exactly once in plan.files (not duplicated)."""
    cover_resource = InputResource(
        href="OEBPS/cover.jpg", media_type="image/jpeg", data=b"\xff\xd8"
    )
    books = [
        _book(
            0,
            "Title Page",
            resources=(cover_resource,),
            cover_href="OEBPS/cover.jpg",
            cover_media_type="image/jpeg",
        )
    ]
    plan = build_merge_plan(books, MergeOptions())

    # Count how many times cover.jpg appears as a file
    cover_count = sum(1 for path in plan.files if "cover.jpg" in path)
    assert cover_count == 1


def test_plan_without_cover_has_no_cover_guide_entry() -> None:
    """No cover option and survivor has no cover → no cover guide entry or cover.xhtml.

    The guide still carries its unconditional type="text" start-of-content entry
    (AT-NAV-1) — only the cover-specific entry and file are absent.
    """
    books = [_book(0, "Chapter 1")]
    plan = build_merge_plan(books, MergeOptions())

    assert [e for e in plan.package.guide if e.type == "cover"] == []
    text_entries = [e for e in plan.package.guide if e.type == "text"]
    assert len(text_entries) == 1
    assert "cover.xhtml" not in plan.files


def test_plan_stamps_single_chapter_book_url() -> None:
    """A plan built from one one-chapter book with book_urls stamps that URL."""
    from ebookerr_sdk.epub.xhtml import ChapterDocument

    # Create proper XHTML with head and body for stamping
    xhtml = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body><p>Content</p></body>
</html>"""
    chapter = InputChapter(
        label="Chapter 1",
        href="OEBPS/file0000.xhtml",
        item_id="file0000",
        media_type="application/xhtml+xml",
        xhtml=xhtml.encode("utf-8"),
    )
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
        chapters=(chapter,),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )
    plan = build_merge_plan([book], MergeOptions(), book_urls=["https://book/one"])

    # Find the chapter file in the plan
    chapter_file = None
    for fname in plan.files:
        if fname.endswith(".xhtml") and fname != "toc.ncx":
            chapter_file = plan.files[fname]
            break

    assert chapter_file is not None
    doc = ChapterDocument.parse(chapter_file.decode("utf-8"))
    assert doc.meta("chapterurl") == "https://book/one"


def test_plan_stamps_multi_chapter_when_none_declared() -> None:
    """A three-chapter book declaring nothing stamps the book URL onto all three."""

    from ebookerr_sdk.epub.xhtml import ChapterDocument

    # Create chapters with proper XHTML
    chapters_list = [
        InputChapter(
            label=f"Chapter {i + 1}",
            href=f"OEBPS/file{i:04d}.xhtml",
            item_id=f"file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=_make_proper_xhtml(None).encode("utf-8"),
        )
        for i in range(3)
    ]
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
        chapters=tuple(chapters_list),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )
    plan = build_merge_plan([book], MergeOptions(), book_urls=["https://book/multi"])

    # Check all chapter files have the stamped URL
    for fname in sorted(plan.files):
        if fname.endswith(".xhtml") and fname != "toc.ncx":
            chapter_xhtml = plan.files[fname]
            doc = ChapterDocument.parse(chapter_xhtml.decode("utf-8"))
            assert doc.meta("chapterurl") == "https://book/multi"


def test_plan_preserves_declared_chapter_urls() -> None:
    """A three-chapter book where each chapter declares its own URL preserves them."""
    from ebookerr_sdk.epub.xhtml import ChapterDocument

    urls = ["https://chapter/1", "https://chapter/2", "https://chapter/3"]
    chapters_list = [
        InputChapter(
            label=f"Chapter {i + 1}",
            href=f"OEBPS/file{i:04d}.xhtml",
            item_id=f"file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=_make_proper_xhtml(url).encode("utf-8"),
        )
        for i, url in enumerate(urls)
    ]
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
        chapters=tuple(chapters_list),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )
    plan = build_merge_plan([book], MergeOptions(), book_urls=["https://book/ignored"])

    # The chapters should preserve their original URLs
    file_list = sorted([f for f in plan.files if f.endswith(".xhtml") and f != "toc.ncx"])
    for i, fname in enumerate(file_list):
        chapter_xhtml = plan.files[fname]
        doc = ChapterDocument.parse(chapter_xhtml.decode("utf-8"))
        # Should preserve the declared URLs, not stamp the book URL
        assert doc.meta("chapterurl") == urls[i]


def test_plan_falls_back_to_dc_source() -> None:
    """book_urls=() and InputBook.source yields chapters declaring that source."""

    from ebookerr_sdk.epub.xhtml import ChapterDocument

    # Create chapters with proper XHTML
    chapters_list = [
        InputChapter(
            label=f"Chapter {i + 1}",
            href=f"OEBPS/file{i:04d}.xhtml",
            item_id=f"file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=_make_proper_xhtml(None).encode("utf-8"),
        )
        for i in range(2)
    ]
    book = InputBook(
        index=0,
        name="book0.epub",
        version="2.0",
        title="Book 0",
        creators=(),
        contributors=(),
        language="en",
        identifier="id-0",
        source="https://epub/src",
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root="OEBPS",
        chapters=tuple(chapters_list),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )
    plan = build_merge_plan([book], MergeOptions(), book_urls=())

    # Check chapters have the source URL stamped
    for fname in sorted(plan.files):
        if fname.endswith(".xhtml") and fname != "toc.ncx":
            chapter_xhtml = plan.files[fname]
            doc = ChapterDocument.parse(chapter_xhtml.decode("utf-8"))
            assert doc.meta("chapterurl") == "https://epub/src"


def test_plan_stamps_each_book_with_its_own_url() -> None:
    """Two books with different book_urls stamp their own URLs onto their chapters."""
    from ebookerr_sdk.epub.xhtml import ChapterDocument

    # Book 0 with 2 chapters
    book0_chapters = [
        InputChapter(
            label=f"A{i + 1}",
            href=f"OEBPS/b0_file{i:04d}.xhtml",
            item_id=f"b0_file{i:04d}",
            media_type="application/xhtml+xml",
            xhtml=_make_proper_xhtml(None).encode("utf-8"),
        )
        for i in range(2)
    ]
    book0 = InputBook(
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
        chapters=tuple(book0_chapters),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )

    # Book 1 with 1 chapter
    book1_chapters = [
        InputChapter(
            label="B1",
            href="OEBPS/b1_file0000.xhtml",
            item_id="b1_file0000",
            media_type="application/xhtml+xml",
            xhtml=_make_proper_xhtml(None).encode("utf-8"),
        )
    ]
    book1 = InputBook(
        index=1,
        name="book1.epub",
        version="2.0",
        title="Book 1",
        creators=(),
        contributors=(),
        language="en",
        identifier="id-1",
        source=None,
        rights=None,
        publisher=None,
        subjects=(),
        dates=(),
        page_direction="ltr",
        content_root="OEBPS",
        chapters=tuple(book1_chapters),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )

    plan = build_merge_plan([book0, book1], MergeOptions(), book_urls=["https://a", "https://b"])

    expected = {0: "https://a", 1: "https://b"}
    assert len(plan.chapters) == 3
    for chapter in plan.chapters:
        doc = ChapterDocument.parse(plan.files[chapter.filename].decode("utf-8"))
        assert doc.meta("chapterurl") == expected[chapter.book_index]


def test_plan_stamping_runs_after_link_rewrite() -> None:
    """A chapter with no URL gets stamped after link rewriting."""
    from ebookerr_sdk.epub.xhtml import ChapterDocument

    chapter = InputChapter(
        label="Chapter 1",
        href="OEBPS/file0000.xhtml",
        item_id="file0000",
        media_type="application/xhtml+xml",
        xhtml=_make_proper_xhtml(None).encode("utf-8"),
    )
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
        chapters=(chapter,),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )
    plan = build_merge_plan([book], MergeOptions(), book_urls=["https://book/url"])

    # The chapter should have the stamped URL
    for fname in plan.files:
        if fname.endswith(".xhtml") and fname != "toc.ncx":
            doc = ChapterDocument.parse(plan.files[fname].decode("utf-8"))
            # Check for the stamped URL
            assert doc.meta("chapterurl") == "https://book/url"


def test_plan_stamping_survives_stylesheet_pass() -> None:
    """The stamped chapterurl meta is present after apply_canonical_stylesheets."""
    from ebookerr_sdk.epub.xhtml import ChapterDocument

    resource = InputResource(href="OEBPS/style.css", media_type="text/css", data=b"body{}")
    chapter = InputChapter(
        label="Chapter 1",
        href="OEBPS/file0000.xhtml",
        item_id="file0000",
        media_type="application/xhtml+xml",
        xhtml=_make_proper_xhtml(None).encode("utf-8"),
    )
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
        chapters=(chapter,),
        resources=(resource,),
        stylesheets=("OEBPS/style.css",),
        cover_href=None,
        cover_media_type=None,
    )
    plan = build_merge_plan([book], MergeOptions(), book_urls=["https://book/url"])

    # Extract and verify the final chapter in the plan still has the stamped URL
    for fname in plan.files:
        if fname.endswith(".xhtml") and fname != "toc.ncx":
            doc = ChapterDocument.parse(plan.files[fname].decode("utf-8"))
            assert doc.meta("chapterurl") == "https://book/url"


def test_title_page_is_excluded_by_manifest_id() -> None:
    """Title page identification uses manifest id (CHC-D9), not filename suffix.

    An InputChapter with item_id="titlepage" is excluded regardless of href.
    An InputChapter with item_id="chap1" is kept even if href ends with "title_page_story.xhtml".
    """
    title_page_chapter = InputChapter(
        label="Title Page",
        href="OEBPS/front.xhtml",
        item_id="titlepage",
        media_type="application/xhtml+xml",
        xhtml=b"<html><body>Title</body></html>",
    )
    regular_chapter = InputChapter(
        label="Chapter 1",
        href="OEBPS/title_page_story.xhtml",
        item_id="chap1",
        media_type="application/xhtml+xml",
        xhtml=b"<html><body>Story</body></html>",
    )
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
        chapters=(title_page_chapter, regular_chapter),
        resources=(),
        stylesheets=(),
        cover_href=None,
        cover_media_type=None,
    )
    plan = build_merge_plan([book], MergeOptions())

    # The title page (by id) should be excluded, but the chapter with the
    # misleading filename should be kept. Only the regular chapter survives.
    chapter_labels = [ch.label for ch in plan.chapters]
    assert "Title Page" not in chapter_labels
    assert "Chapter 1" in chapter_labels
    assert len(plan.chapters) == 1
