"""Assemble the merge plan: manifest, spine, TOC and minimal metadata.

This is the pure assembly step of the EPUB merge pipeline: it takes the neutral
input books and the planned chapters/resources (from
:mod:`epub_merge.merge.chapters` and :mod:`epub_merge.merge.assets`)
and combines them into a single :class:`MergePlan` describing the merged output
book, with no I/O and no serialisation. Later feature cards extend
:func:`build_merge_plan` in place to add richer metadata (description, source,
rights, publisher, contributors), covers, the EPUB3 branch, landmarks, the guide,
and href rewriting.
"""

from __future__ import annotations

import logging
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ebookerr_sdk.domain.chapter_naming import slugify
from ebookerr_sdk.epub.assets import AssetRegistry
from ebookerr_sdk.epub.builder import (
    GuideEntry,
    LandmarkEntry,
    ManifestEntry,
    MetadataSpec,
    PackageSpec,
    SpineEntry,
    TocEntry,
)
from ebookerr_sdk.epub.errors import EpubError
from ebookerr_sdk.epub.roles import TITLE_PAGE_ITEM_IDS
from ebookerr_sdk.epub.xhtml import ChapterDocument

from epub_merge.merge.assets import AssetPlan, plan_assets
from epub_merge.merge.chapter_urls import stamp_chapter_urls
from epub_merge.merge.chapters import plan_chapters
from epub_merge.merge.cover import CoverPlan, plan_cover
from epub_merge.merge.landmarks import build_landmarks, first_narrative_href
from epub_merge.merge.links import rewrite_chapter_links
from epub_merge.merge.metadata import (
    build_description,
    merge_contributors,
    merge_creators,
    merge_subjects,
    resolve_language,
    resolve_page_direction,
    rewrite_book_title,
    survivor_value,
)
from epub_merge.merge.model import (
    NAV_HREF,
    NCX_HREF,
    InputBook,
    MergeOptions,
    PlannedChapter,
)
from epub_merge.merge.pages import apply_title_page
from epub_merge.merge.styles import (
    apply_canonical_stylesheets,
    canonical_stylesheets,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Everything the merged EPUB will contain, before it is serialised.

    Attributes:
        version: Output package version, ``"2.0"`` or ``"3.0"``.
        title: The output book title (also the NCX ``docTitle``).
        identifier: The output's unique identifier, e.g. ``"urn:uuid:<uuid4>"``.
        files: Output path (OPF-relative) to bytes, for every chapter and resource.
        package: The package-document description.
        chapters: Planned chapters in reading order, with metadata (for testing/stamping).
        toc: Flat TOC entries in reading order.
        landmarks: EPUB3 landmark entries (empty for now).
    """

    version: str
    title: str
    identifier: str
    files: dict[str, bytes]
    package: PackageSpec
    chapters: Sequence[PlannedChapter]
    toc: tuple[TocEntry, ...]
    landmarks: tuple[LandmarkEntry, ...] = ()


def _resource_id(path: str, taken: set[str]) -> str:
    """Derive a unique, valid XML NCName manifest id for a resource path.

    Args:
        path: The resource's output path (OPF-relative).
        taken: Ids already assigned; updated in place with the returned id.

    Returns:
        A NCName-valid id, unique against ``taken``.
    """
    slug = slugify(path)
    if not slug:
        slug = "resource"
    if slug[0].isdigit():
        slug = f"_{slug}"

    candidate = slug
    suffix = 2
    while candidate in taken:
        candidate = f"{slug}_{suffix}"
        suffix += 1

    taken.add(candidate)
    return candidate


def _chapter_properties(chapter: PlannedChapter) -> str:
    """Space-separated EPUB3 ``properties`` this chapter earns (``MR-PROP-1``); ``""`` when none.

    Scans the chapter's XHTML content for SVG, MathML, and script elements, returning
    the EPUB3 manifest properties as a space-separated, alphabetically sorted string.

    Args:
        chapter: The planned chapter to scan.

    Returns:
        A space-separated string of properties (e.g., ``"mathml scripted"``), or ``""``.
        On parse error, logs a warning and returns ``""``.
    """
    try:
        document = ChapterDocument.parse(chapter.xhtml.decode("utf-8"))
        props = document.content_properties()
        if props:
            properties = " ".join(sorted(props))
            logger.debug(
                'Merge chapter "%s" declares EPUB3 properties: %s',
                chapter.filename,
                properties,
            )
            return properties
        return ""
    except (EpubError, UnicodeDecodeError, ET.ParseError) as exc:
        logger.warning(
            'Merge could not scan "%s" for EPUB3 properties: %s',
            chapter.filename,
            exc,
        )
        return ""


def _output_version(books: Sequence[InputBook]) -> str:
    """Select the highest EPUB version from input books.

    Args:
        books: Input books in merge order.

    Returns:
        ``"3.0"`` if any input is EPUB3, otherwise ``"2.0"`` (MR-EPUB-1).
    """
    return "3.0" if any(book.version.startswith("3") for book in books) else "2.0"


def _build_guide_and_landmarks(
    chapters: Sequence[PlannedChapter],
    cover_path: str | None,
    version: str,
) -> tuple[tuple[GuideEntry, ...], tuple[LandmarkEntry, ...]]:
    """Build guide entries (both versions) and landmarks (EPUB3 only).

    Args:
        chapters: Chapters in reading order.
        cover_path: Cover page path if present, else None.
        version: Output EPUB version.

    Returns:
        Tuple of (guide_entries, landmarks_entries).
    """
    guide_entries = []
    if cover_path:
        guide_entries.append(GuideEntry("cover", "Cover", cover_path))
    narrative_href = first_narrative_href(chapters)
    if narrative_href is not None:
        guide_entries.append(GuideEntry("text", "Start of content", narrative_href))
    guide = tuple(guide_entries)

    landmarks = build_landmarks(chapters, cover_href=cover_path) if version.startswith("3") else ()
    return guide, landmarks


def _exclude_title_page_from_books(books: Sequence[InputBook]) -> Sequence[InputBook]:
    """Create a copy of books with title page resources and auto-gen title pages excluded.

    Excludes:
    1. Title page resources (for asset deduplication when rewriting).
    2. Auto-generated title page chapters from ALL books (not just non-survivors) to prevent
       duplicate manifest hrefs (SH-4, MR-DEDUP-1). The title page is identified by manifest id
       (CHC-D9), the same rule the reader and roles module apply. The title page is re-added by
       apply_title_page if needed.

    This allows plan_chapters to work with user-provided chapters only, avoiding
    collisions when multiple input books have the same auto-generated title page href.

    Args:
        books: Input books in merge order.

    Returns:
        A sequence of InputBook objects with title page resources and auto-gen chapters removed.
    """
    result: list[InputBook] = []
    for book in books:
        # Exclude resources that match common title page paths/ids
        filtered_resources = tuple(
            r
            for r in book.resources
            if not (
                "title" in r.href.lower()
                and (r.href.endswith(".xhtml") or r.href.endswith(".html"))
            )
        )

        # Exclude auto-gen title page chapters from ALL books to prevent duplicate
        # manifest hrefs (SH-4, MR-DEDUP-1). The title page is identified by manifest id
        # (CHC-D9), the same rule the reader and roles module apply.
        filtered_chapters = tuple(
            ch for ch in book.chapters if ch.item_id.lower() not in TITLE_PAGE_ITEM_IDS
        )

        updated = replace(book, resources=filtered_resources, chapters=filtered_chapters)
        result.append(updated)

    return result


def _epub3_extras(version: str) -> tuple[str | None, bool]:
    """Generate EPUB3-specific metadata when applicable.

    Args:
        version: The output EPUB version.

    Returns:
        A tuple of (modified_timestamp, should_add_nav_entry) where modified_timestamp
        is an ISO 8601 string (``"YYYY-MM-DDTHH:MM:SSZ"``) when version is 3.0,
        or None otherwise (MR-MODIFIED-1).
    """
    if version.startswith("3"):
        modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return modified, True
    return None, False


def _build_manifest(
    chapters: Sequence[PlannedChapter],
    assets: AssetPlan,
    cover_image_path: str | None = None,
    cover_media_type: str | None = None,
    cover_is_new: bool = False,
    version: str = "2.0",
) -> tuple[tuple[ManifestEntry, ...], tuple[str, str] | None]:
    """Build the manifest: the NCX, then chapters, then resources (sorted).

    Args:
        chapters: Planned chapters in reading order.
        assets: The planned resource set.
        cover_image_path: OPF-relative path to the cover image, or None.
        cover_media_type: Media type of the cover image, or None.
        cover_is_new: Whether the cover image is being added (True) or reused (False).
        version: The output EPUB version ("2.0" or "3.0").

    Returns:
        A tuple of (manifest_entries, cover_item_id_pair) where cover_item_id_pair is
        (item_id, image_path) when the cover is new and needs a manifest entry, or None.
    """
    entries: list[ManifestEntry] = [ManifestEntry("ncx", NCX_HREF, "application/x-dtbncx+xml")]

    if version.startswith("3"):
        entries.append(ManifestEntry("nav", NAV_HREF, "application/xhtml+xml", properties="nav"))

    for chapter in chapters:
        properties = _chapter_properties(chapter) if version.startswith("3") else ""
        entries.append(
            ManifestEntry(
                item_id=chapter.item_id,
                href=chapter.filename,
                media_type="application/xhtml+xml",
                properties=properties,
            )
        )

    taken = {"ncx"} | {chapter.item_id for chapter in chapters}

    # If this is a new cover image, reserve its id now
    cover_image_id = None
    if cover_is_new and cover_image_path:
        cover_image_id = _resource_id(cover_image_path, taken)

    for path in sorted(assets.files):
        entries.append(
            ManifestEntry(
                item_id=_resource_id(path, taken),
                href=path,
                media_type=assets.media_types.get(path, "application/octet-stream"),
            )
        )

    # Return entries and info about the cover image if it's new
    cover_pair: tuple[str, str] | None = (
        (cover_image_id, cover_image_path)
        if cover_is_new and cover_image_id and cover_image_path
        else None
    )
    return tuple(entries), cover_pair


def _resolve_cover_item_id(
    cover: CoverPlan | None,
    cover_image_pair: tuple[str, str] | None,
    path_to_id: dict[str, str],
) -> str | None:
    """Resolve the merged book's cover image manifest item id, if any.

    Args:
        cover: The planned cover, or None when the merge has no cover.
        cover_image_pair: The (item_id, image_path) pair reserved by
            :func:`_build_manifest` for a newly added cover image, or None.
        path_to_id: Existing resource paths mapped to their manifest item id,
            used to look up a reused (non-new) cover image's id.

    Returns:
        The cover image's manifest item id, or None when there is no cover.
    """
    if not cover:
        return None
    if cover.extra_files:
        # New cover image - use the id reserved for it.
        return cover_image_pair[0] if cover_image_pair else None
    # Reused existing image - find its id from the manifest.
    return path_to_id.get(cover.image_path)


def _reserved_chapter_stems(paths: Iterable[str]) -> set[str]:
    """The chapter-filename stems that would collide with these already-taken output paths.

    Args:
        paths: Output paths already claimed or reserved (system documents,
            carried resources) before chapter names are allocated.

    Returns:
        The set of filename stems (no extension) those paths would collide with.
        Paths with a directory component are skipped: chapter filenames are
        always flat (``MR-FLAT-1``), so a nested resource cannot collide.
    """
    stems: set[str] = set()
    for path in paths:
        # Chapter filenames are always flat (MR-FLAT-1), so a nested resource cannot collide.
        if "/" in path:
            continue
        stem, dot, _ext = path.rpartition(".")
        stems.add(stem if dot else path)
    return stems


def _free_item_id(preferred: str, taken: set[str]) -> str:
    """Find a free manifest item id, preferring ``preferred``, never slugified.

    Args:
        preferred: The desired manifest item id.
        taken: Ids already assigned in the manifest being built.

    Returns:
        ``preferred`` when free, else ``preferred_2``, ``preferred_3``, and so on.
    """
    if preferred not in taken:
        return preferred
    suffix = 2
    while f"{preferred}_{suffix}" in taken:
        suffix += 1
    return f"{preferred}_{suffix}"


def _plan_cover_and_assemble_output(
    books: Sequence[InputBook],
    options: MergeOptions,
    chapters: Sequence[PlannedChapter],
    assets: AssetPlan,
    registry: AssetRegistry,
    version: str,
    should_add_nav: bool,
) -> tuple[
    dict[str, bytes],
    tuple[SpineEntry, ...],
    tuple[ManifestEntry, ...],
    tuple[GuideEntry, ...],
    tuple[LandmarkEntry, ...],
    tuple[TocEntry, ...],
    str | None,
]:
    """Plan the cover, then assemble the files, spine, manifest, guide, landmarks and TOC.

    A single helper because every one of these outputs needs to know whether — and where —
    the cover landed, and the cover itself is planned here.

    Args:
        books: Input books in merge order; ``books[0]`` is the survivor.
        options: Merge options (cover selection, etc.).
        chapters: The finalised chapter plan.
        assets: The planned resource set.
        registry: The shared path registry every allocator reserves through.
        version: The output EPUB version ("2.0" or "3.0").
        should_add_nav: Whether the EPUB3 nav document is included.

    Returns:
        ``(files, spine, manifest, guide, landmarks, toc, cover_item_id)``.
    """
    cover = plan_cover(books, options, assets, registry=registry)

    # Build the manifest with knowledge of cover for id assignment.
    manifest, cover_image_pair = _build_manifest(
        chapters,
        assets,
        cover_image_path=cover.image_path if cover else None,
        cover_media_type=cover.image_media_type if cover else None,
        cover_is_new=bool(cover and cover.extra_files),
        version=version if should_add_nav else "2.0",
    )

    # Build a path-to-item-id map for existing resources, to determine cover item id.
    path_to_id: dict[str, str] = {}
    for entry in manifest:
        if entry.media_type not in ("application/xhtml+xml", "application/x-dtbncx+xml"):
            path_to_id[entry.href] = entry.item_id
    cover_item_id = _resolve_cover_item_id(cover, cover_image_pair, path_to_id)

    # The cover page's item id is preferred-then-suffixed, never slugified: "cover" belongs to the
    # role vocabulary (roles.NON_CHAPTER_ITEM_IDS) and must survive every ordinary merge.
    taken_item_ids = {entry.item_id for entry in manifest}
    if cover_image_pair:
        taken_item_ids.add(cover_image_pair[0])
    cover_page_item_id = _free_item_id("cover", taken_item_ids)
    if cover is not None and cover_page_item_id != "cover":
        logger.info("Merge cover page took a free item id: %s", cover_page_item_id)

    # Build files dict.
    files = {chapter.filename: chapter.xhtml for chapter in chapters} | assets.files
    if cover:
        files.update(cover.extra_files)
        files[cover.page_path] = cover.page_data

    # Build spine - insert cover as first entry if present.
    spine_entries = [SpineEntry(chapter.item_id, "yes") for chapter in chapters]
    if cover:
        spine_entries.insert(0, SpineEntry(cover_page_item_id, "yes"))
    spine = tuple(spine_entries)

    # Build guide (both versions) and landmarks (EPUB3 only).
    cover_path = cover.page_path if cover else None
    guide, landmarks = _build_guide_and_landmarks(chapters, cover_path, version)

    # Build TOC - no cover entry.
    toc = tuple(TocEntry(chapter.label, chapter.filename) for chapter in chapters)

    # Build manifest with cover entries.
    manifest_list: list[ManifestEntry] = list(manifest)
    if cover:
        # Add cover page entry.
        manifest_list.append(
            ManifestEntry(cover_page_item_id, cover.page_path, "application/xhtml+xml")
        )
        # Add cover image entry only if it's new.
        if cover.extra_files and cover_image_pair:
            cover_properties = "cover-image" if version.startswith("3") else ""
            manifest_list.append(
                ManifestEntry(
                    item_id=cover_image_pair[0],
                    href=cover.image_path,
                    media_type=cover.image_media_type,
                    properties=cover_properties,
                )
            )
    final_manifest = tuple(manifest_list)

    return files, spine, final_manifest, guide, landmarks, toc, cover_item_id


def build_merge_plan(
    books: Sequence[InputBook],
    options: MergeOptions,
    *,
    book_urls: Sequence[str | None] = (),
) -> MergePlan:
    """Turn the input books into a complete description of the merged output (pure).

    Every output path — the NCX, the EPUB3 nav, every carried resource, every chapter
    and every generated page — is allocated through one :class:`AssetRegistry`, so two
    allocators can never mint the same href.

    Args:
        books: Input books in merge order; ``books[0]`` is the survivor.
        options: Merge options (title/metadata overrides, cover, etc.).
        book_urls: The core's own story URL for each input, aligned by position with
            ``[target, *sources]``. Used to stamp a chapter URL onto merged chapters that
            declare none; a missing or ``None`` entry falls back to the URL the input EPUB
            itself declares.

    Returns:
        A MergePlan describing the manifest, spine, TOC and minimal metadata
        for the merged output book.
    """
    # Exclude auto-generated title pages from all books to prevent duplicate manifest hrefs
    # (SH-4, MR-DEDUP-1). The title pages are handled separately by apply_title_page.
    # User-provided "Title Page" chapters (which are regular chapters, not the auto-gen one)
    # are preserved and merged normally.
    survivor_title_page = books[0].title_page if books else None
    books_without_auto_title_pages = list(_exclude_title_page_from_books(books))

    # When not regenerating (MR-PARAM-1 off), carry the survivor's original title
    # page through the ordinary chapter pipeline (naming, link/stylesheet rewriting)
    # instead of dropping it — "off" means copy, not omit.
    if not options.rewrite_title_page and survivor_title_page is not None:
        survivor = books_without_auto_title_pages[0]
        books_without_auto_title_pages[0] = replace(
            survivor, chapters=(survivor_title_page, *survivor.chapters)
        )

    # One registry owns every output path in the merged book. The package document always writes
    # toc.ncx (and nav.xhtml on EPUB3) itself, so those names are reserved BEFORE anything claims:
    # a carried resource of the same name is then claimed at toc_1.ncx / nav_1.xhtml and
    # assets.mapping records the rename, so rewrite_chapter_links follows it automatically.
    version = _output_version(books)
    registry = AssetRegistry()
    system_paths = [registry.reserve(NCX_HREF)]
    if version.startswith("3"):
        system_paths.append(registry.reserve(NAV_HREF))
    logger.debug("Merge reserved %d system path(s) for version %s", len(system_paths), version)

    assets, registry = plan_assets(books_without_auto_title_pages, registry=registry)

    # Chapter names are allocated against the same authority, so a chapter labelled "Cover",
    # "Nav" or "Notes" can never take a path a resource or a system document already owns.
    chapters = list(
        plan_chapters(
            books_without_auto_title_pages,
            reserved=_reserved_chapter_stems([*system_paths, *assets.files]),
        )
    )
    for chapter in chapters:
        registry.reserve(chapter.filename)
    logger.debug("Merge reserved %d chapter path(s) against generated pages", len(chapters))

    chapters = rewrite_chapter_links(chapters, assets)
    chapters = stamp_chapter_urls(chapters, books_without_auto_title_pages, book_urls)
    chapters = apply_canonical_stylesheets(chapters, canonical_stylesheets(books, assets))

    title = books[0].title
    identifier = f"urn:uuid:{uuid.uuid4()}"

    # Rewrite the book title with chapter range if requested (MR-PARAM-2)
    if options.rewrite_book_title:
        title = rewrite_book_title(books[0].title, chapters) or books[0].title

    # Apply regenerated title page if requested (MR-PARAM-1)
    if options.rewrite_title_page:
        chapters = apply_title_page(
            chapters,
            title=title,
            creators=merge_creators(books),
            subjects=merge_subjects(books),
            language=resolve_language(books, options.language),
            stylesheets=canonical_stylesheets(books, assets),
            registry=registry,
        )

    logger.debug(
        "Merge output version %s selected from inputs: %s",
        version,
        [b.version for b in books],
    )

    modified, should_add_nav = _epub3_extras(version)

    files, spine, final_manifest, guide, landmarks, toc, cover_item_id = (
        _plan_cover_and_assemble_output(
            books, options, chapters, assets, registry, version, should_add_nav
        )
    )

    metadata = MetadataSpec(
        title=title,
        identifier=identifier,
        language=resolve_language(books, options.language),
        creators=merge_creators(books),
        contributors=merge_contributors(books),
        subjects=merge_subjects(books),
        dates=books[0].dates,
        description=build_description(books, options.description) or None,
        source=survivor_value(books, "source", options.source),
        rights=survivor_value(books, "rights", options.rights),
        publisher=survivor_value(books, "publisher", options.publisher),
        cover_item_id=cover_item_id,
        modified=modified,
    )

    package = PackageSpec(
        version=version,
        metadata=metadata,
        manifest=final_manifest,
        spine=spine,
        guide=guide,
        page_progression_direction=resolve_page_direction(books),
    )

    logger.info(
        'Merge plan built: title="%s", version=%s, %d chapter(s), %d resource(s)',
        title,
        version,
        len(chapters),
        len(assets.files),
    )
    logger.debug("Merge output identifier: %s", identifier)

    return MergePlan(
        version=version,
        title=title,
        identifier=identifier,
        files=files,
        package=package,
        chapters=chapters,
        toc=toc,
        landmarks=landmarks,
    )
