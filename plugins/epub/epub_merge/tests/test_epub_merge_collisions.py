"""Tests pinning that every output path in a merge is allocated through one registry.

Before this fix, chapter filename allocation (``plan_chapter_filenames``) and the
hardcoded ncx/nav/cover manifest entries in ``build_merge_plan`` each claimed names
the other allocator never saw, so a chapter labelled "Cover", "Nav" or "Notes" — or a
carried resource sharing a name with a system document — could collide with a
generated page. Case letters match the investigation that reproduced them.
"""

from __future__ import annotations

from epub_merge.merge.model import InputBook, InputChapter, InputResource, MergeOptions
from epub_merge.merge.plan import build_merge_plan
from epub_merge.merge.validate import validate_plan


def _book(
    index: int,
    *labels: str,
    version: str = "2.0",
    resources: tuple[InputResource, ...] = (),
    cover_href: str | None = None,
    cover_media_type: str | None = None,
    content_root: str = "OEBPS",
) -> InputBook:
    """Build a minimal InputBook whose chapters carry the given labels."""
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
        version=version,
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
        content_root=content_root,
        chapters=chapters,
        resources=resources,
        stylesheets=(),
        cover_href=cover_href,
        cover_media_type=cover_media_type,
    )


def test_chapter_labelled_cover_does_not_collide_with_the_generated_cover_page() -> None:
    """A chapter labelled "Cover" keeps cover.xhtml; the generated cover page moves (case A)."""
    survivor = _book(
        0,
        "Cover",
        "Chapter 1",
        resources=(InputResource("OEBPS/cover-image.png", "image/png", b"PNG"),),
        cover_href="OEBPS/cover-image.png",
        cover_media_type="image/png",
    )
    source = _book(1, "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions())

    validate_plan(plan, expected_chapters=3)

    hrefs = [entry.href for entry in plan.package.manifest]
    ids = [entry.item_id for entry in plan.package.manifest]
    assert len(hrefs) == len(set(hrefs))
    assert len(ids) == len(set(ids))

    chapter_filenames = {chapter.filename for chapter in plan.chapters}
    assert "cover.xhtml" in chapter_filenames
    assert "cover_1.xhtml" in hrefs
    assert "cover_1.xhtml" not in chapter_filenames


def test_the_cover_page_id_matches_its_spine_entry_when_it_had_to_move() -> None:
    """The moved cover page's manifest id matches its spine idref (case A)."""
    survivor = _book(
        0,
        "Cover",
        "Chapter 1",
        resources=(InputResource("OEBPS/cover-image.png", "image/png", b"PNG"),),
        cover_href="OEBPS/cover-image.png",
        cover_media_type="image/png",
    )
    source = _book(1, "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions())

    cover_page_entry = next(
        (entry for entry in plan.package.manifest if entry.href == "cover_1.xhtml"), None
    )
    assert cover_page_entry is not None
    assert cover_page_entry.item_id == "cover_2"
    assert plan.package.spine[0].idref == "cover_2"


def test_a_cover_page_that_did_not_move_keeps_the_plain_id() -> None:
    """An ordinary merge with no name collision keeps the cover page at item_id "cover"."""
    survivor = _book(
        0,
        "Chapter 1",
        resources=(InputResource("OEBPS/cover-image.png", "image/png", b"PNG"),),
        cover_href="OEBPS/cover-image.png",
        cover_media_type="image/png",
    )
    source = _book(1, "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions())

    cover_page_entry = next(
        (entry for entry in plan.package.manifest if entry.href == "cover.xhtml"), None
    )
    assert cover_page_entry is not None
    assert cover_page_entry.item_id == "cover"


def test_epub3_chapter_labelled_nav_does_not_collide_with_the_nav_document() -> None:
    """A chapter labelled "Nav" does not take the EPUB3 nav document's path (case B)."""
    survivor = _book(0, "Nav", "Chapter 1", version="3.0")
    source = _book(1, "Chapter 2", version="3.0")

    plan = build_merge_plan([survivor, source], MergeOptions())

    validate_plan(plan, expected_chapters=3)
    ids = [entry.item_id for entry in plan.package.manifest]
    assert len(ids) == len(set(ids))

    nav_chapter = next((chapter for chapter in plan.chapters if chapter.label == "Nav"), None)
    assert nav_chapter is not None
    assert nav_chapter.filename != "nav.xhtml"

    nav_entry = next((entry for entry in plan.package.manifest if entry.item_id == "nav"), None)
    assert nav_entry is not None
    assert nav_entry.href == "nav.xhtml"


def test_resource_named_nav_xhtml_does_not_collide_with_the_nav_document() -> None:
    """A carried resource literally named nav.xhtml is renamed away (case C)."""
    survivor = _book(
        0,
        "Chapter 1",
        version="3.0",
        resources=(InputResource("OEBPS/nav.xhtml", "application/xhtml+xml", b"<html>nav</html>"),),
    )
    source = _book(1, "Chapter 2", version="3.0")

    plan = build_merge_plan([survivor, source], MergeOptions())

    validate_plan(plan, expected_chapters=2)
    assert "nav_1.xhtml" in {entry.href for entry in plan.package.manifest}


def test_resource_named_toc_ncx_does_not_collide_with_the_ncx() -> None:
    """A carried resource literally named toc.ncx at a non-NCX media type is renamed (case D)."""
    survivor = _book(
        0,
        "Chapter 1",
        resources=(InputResource("OEBPS/toc.ncx", "application/xml", b"<x/>"),),
    )
    source = _book(1, "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions())

    validate_plan(plan, expected_chapters=2)
    assert "toc_1.ncx" in {entry.href for entry in plan.package.manifest}


def test_chapter_labelled_cover_with_title_page_rewrite_does_not_collide() -> None:
    """Case A still holds when a regenerated title page is also being added (case E)."""
    survivor = _book(
        0,
        "Cover",
        "Chapter 1",
        resources=(InputResource("OEBPS/cover-image.png", "image/png", b"PNG"),),
        cover_href="OEBPS/cover-image.png",
        cover_media_type="image/png",
    )
    source = _book(1, "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions(rewrite_title_page=True))

    validate_plan(plan, expected_chapters=3)
    hrefs = [entry.href for entry in plan.package.manifest]
    ids = [entry.item_id for entry in plan.package.manifest]
    assert len(hrefs) == len(set(hrefs))
    assert len(ids) == len(set(ids))


def test_a_stale_cover_resource_and_a_chapter_labelled_cover_do_not_collide() -> None:
    """A stale carried cover.xhtml resource and a chapter labelled "Cover" coexist (case G)."""
    survivor = _book(
        0,
        "Chapter 1",
        resources=(
            InputResource("OEBPS/cover.xhtml", "application/xhtml+xml", b"<html>old cover</html>"),
            InputResource("OEBPS/cover-image.png", "image/png", b"PNG"),
        ),
        cover_href="OEBPS/cover-image.png",
        cover_media_type="image/png",
    )
    source = _book(1, "Cover", "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions())

    validate_plan(plan, expected_chapters=3)
    hrefs = [entry.href for entry in plan.package.manifest]
    assert len(hrefs) == len(set(hrefs))


def test_a_carried_resource_and_a_chapter_with_the_same_name_do_not_collide() -> None:
    """A carried notes.xhtml resource and a chapter labelled "Notes" coexist (case H)."""
    survivor = _book(
        0,
        "Chapter 1",
        resources=(
            InputResource("OEBPS/notes.xhtml", "application/xhtml+xml", b"<html>notes</html>"),
        ),
    )
    source = _book(1, "Notes", "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions())

    validate_plan(plan, expected_chapters=3)
    notes_chapter = next((chapter for chapter in plan.chapters if chapter.label == "Notes"), None)
    assert notes_chapter is not None
    assert notes_chapter.filename != "notes.xhtml"


def test_reserving_chapter_paths_does_not_add_them_to_asset_files() -> None:
    """Reserving chapter filenames against the registry never leaks into asset files (case A)."""
    survivor = _book(
        0,
        "Cover",
        "Chapter 1",
        resources=(InputResource("OEBPS/cover-image.png", "image/png", b"PNG"),),
        cover_href="OEBPS/cover-image.png",
        cover_media_type="image/png",
    )
    source = _book(1, "Chapter 2")

    plan = build_merge_plan([survivor, source], MergeOptions())

    hrefs = [entry.href for entry in plan.package.manifest]
    for chapter in plan.chapters:
        assert plan.files[chapter.filename] == chapter.xhtml
        assert hrefs.count(chapter.filename) == 1


def test_the_epub3_cover_landmark_points_at_the_real_cover_page() -> None:
    """The EPUB3 cover landmark points at the cover page's actual, possibly-moved path (case A)."""
    survivor = _book(
        0,
        "Cover",
        "Chapter 1",
        version="3.0",
        resources=(InputResource("OEBPS/cover-image.png", "image/png", b"PNG"),),
        cover_href="OEBPS/cover-image.png",
        cover_media_type="image/png",
    )
    source = _book(1, "Chapter 2", version="3.0")

    plan = build_merge_plan([survivor, source], MergeOptions())

    landmark_cover = [entry for entry in plan.landmarks if entry.epub_type == "cover"]
    assert len(landmark_cover) == 1
    assert landmark_cover[0].href == "cover_1.xhtml"

    guide_cover = [entry for entry in plan.package.guide if entry.type == "cover"]
    assert len(guide_cover) == 1
    assert guide_cover[0].href == landmark_cover[0].href


def test_a_renamed_system_resource_is_reachable_from_the_plan_files() -> None:
    """A resource renamed away from the reserved nav.xhtml path is still written out (case C)."""
    survivor = _book(
        0,
        "Chapter 1",
        version="3.0",
        resources=(InputResource("OEBPS/nav.xhtml", "application/xhtml+xml", b"<html>nav</html>"),),
    )
    source = _book(1, "Chapter 2", version="3.0")

    plan = build_merge_plan([survivor, source], MergeOptions())

    assert "nav_1.xhtml" in plan.files
