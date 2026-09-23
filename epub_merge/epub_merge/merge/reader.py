"""Read an input EPUB into the merge's neutral, in-memory model.

This module is the only I/O boundary of the merge — every planning step
downstream is pure and works on InputBook values, which is what makes the
merge unit-testable without fixture files on disk.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from pathlib import Path

from ebookerr_sdk.epub import EpubDocument, EpubError, NavPoint
from ebookerr_sdk.epub.chapters import ChapterRole, classify_spine
from ebookerr_sdk.epub.opf import ManifestItem
from ebookerr_sdk.epub.roles import non_chapter_idrefs, title_page_item
from ebookerr_sdk.epub.titles import resolve_book_title, resolve_nav_title

from epub_merge.merge.errors import MergeInputError
from epub_merge.merge.model import InputBook, InputChapter, InputResource

logger = logging.getLogger(__name__)


def read_input_book(path: Path, index: int) -> InputBook:
    """Read one input EPUB into the merge's neutral, in-memory model.

    Opens an EPUB file from disk, parses its package document, table of
    contents, and chapter content, and populates an InputBook with all
    metadata and content needed for merge planning.

    Args:
        path: Filesystem path to the .epub file.
        index: Book index for the merge (0 is the survivor/target).

    Returns:
        An InputBook with all fields populated.

    Raises:
        MergeInputError: If the EPUB is malformed, missing required
            structure, or has missing chapter files (missing resources
            only warn and are skipped).
    """
    try:
        doc = EpubDocument.open(path)
    except (EpubError, zipfile.BadZipFile) as exc:
        raise MergeInputError(f"{path.name}: {exc}") from exc

    version = doc.opf.package_version()
    title = resolve_book_title(doc)
    creators = tuple(doc.opf.get_creators())
    contributors = tuple(doc.opf.get_contributors())
    language = doc.opf.get_language() or ""
    identifier = doc.opf.unique_identifier() or ""
    source = doc.opf.get_source()
    rights = doc.opf.get_rights()
    publisher = doc.opf.get_publisher()
    subjects = tuple(doc.opf.get_subjects())
    dates = tuple(doc.opf.get_dates())
    page_direction = doc.opf.page_progression_direction()

    chapters = _read_chapters(doc, path)
    chapter_hrefs = [c.href for c in chapters]
    resources = _read_resources(doc, path, chapter_hrefs)
    stylesheets = tuple(item.href for item in doc.opf.manifest() if item.media_type == "text/css")
    content_root = _content_root(chapter_hrefs)
    title_page = _read_title_page(doc)

    cover_item = doc.cover_item()
    cover_href = cover_item.href if cover_item is not None else None
    cover_media_type = cover_item.media_type if cover_item is not None else None

    book = InputBook(
        index=index,
        name=path.name,
        version=version,
        title=title,
        creators=creators,
        contributors=contributors,
        language=language,
        identifier=identifier,
        source=source,
        rights=rights,
        publisher=publisher,
        subjects=subjects,
        dates=dates,
        page_direction=page_direction,
        content_root=content_root,
        chapters=tuple(chapters),
        resources=tuple(resources),
        stylesheets=stylesheets,
        cover_href=cover_href,
        cover_media_type=cover_media_type,
        title_page=title_page,
    )

    logger.debug(
        'Merge input %d: "%s" version=%s, %d chapter(s), %d resource(s), content_root=%r',
        index,
        path.name,
        version,
        len(chapters),
        len(resources),
        content_root,
    )

    return book


def _chapter_label(doc: EpubDocument, labels: dict[str, str], item: ManifestItem) -> str:
    """Resolve one manifest item's chapter label: NCX navMap first, else its own XHTML title.

    Args:
        doc: The parsed EPUB document.
        labels: Chapter hrefs mapped to their NCX navMap label.
        item: The manifest item to label.

    Returns:
        The resolved label (never raises; falls back to "" via resolve_nav_title).
    """
    return labels.get(item.href) or resolve_nav_title(doc, NavPoint(item.id, 0, "", item.href))


def _reject_dangling_spine_refs(doc: EpubDocument, path: Path) -> None:
    """Raise when a content spine entry points at a manifest item that does not exist.

    :func:`~ebookerr_sdk.epub.roles.content_chapter_items` — which
    :meth:`~ebookerr_sdk.epub.document.EpubDocument.content_chapters` and every other
    chapter walk in the app build on — skips a dangling ``idref`` so a broken book can
    never break a pull. A merge input is the one place that is not acceptable: silently
    dropping a chapter would produce a merged book missing content, so the reader
    validates first and fails closed.

    Non-chapter entries are not checked, matching the classification's own scope: a
    ``linear="no"`` entry is excluded before its manifest item is ever looked up.

    Args:
        doc: The parsed EPUB document.
        path: The filesystem path (used for the error message).

    Raises:
        MergeInputError: If a content spine entry references an unknown manifest item.
    """
    excluded = non_chapter_idrefs(doc.opf)
    known = {item.id for item in doc.opf.manifest()}
    for entry in doc.opf.spine():
        if entry.idref not in excluded and entry.idref not in known:
            raise MergeInputError(
                f'{path.name}: spine references unknown manifest item "{entry.idref}"'
            )


def _read_chapters(doc: EpubDocument, path: Path) -> list[InputChapter]:
    """Read the book's content chapters into an ``InputChapter`` list, in spine order.

    Walks :meth:`~ebookerr_sdk.epub.document.EpubDocument.content_chapters` — the one
    content-chapter list the whole app shares — after
    :func:`_reject_dangling_spine_refs` has ruled out a broken spine. Enriches each
    chapter with its editorial role from ``classify_spine``.

    Args:
        doc: The parsed EPUB document.
        path: The filesystem path (used for error messages).

    Returns:
        The content chapters in spine order, excluding non-chapters.

    Raises:
        MergeInputError: If a spine entry references a non-existent manifest item,
            or if a chapter file is missing from the archive.
    """
    _reject_dangling_spine_refs(doc, path)
    labels = {point.src: resolve_nav_title(doc, point) for point in doc.ncx.nav_points()}

    # Build a map from href to role using classify_spine
    classified = classify_spine(doc)
    href_to_role = {entry.href: entry.role for entry in classified}

    chapters: list[InputChapter] = []
    for item in doc.content_chapters():
        member_name = _member(doc, item.href)
        data = doc.member_bytes(member_name)
        if data is None:
            raise MergeInputError(f'{path.name}: missing file "{item.href}"')
        label = _chapter_label(doc, labels, item)
        role = str(href_to_role.get(item.href, ChapterRole.OTHER))
        chapters.append(InputChapter(label, item.href, item.id, item.media_type, data, role))

    return chapters


def _read_title_page(doc: EpubDocument) -> InputChapter | None:
    """Read the book's own title-page chapter, independent of the chapter list.

    ``_read_chapters`` excludes a title page as a non-chapter (role-based,
    like every other chapter classification in this app) — this reads it
    directly, so the survivor's original title page can be carried through
    unchanged by a merge that is not regenerating one (``MR-PARAM-1`` off).
    A missing archive member is treated the same as no title page at all: a
    merge input's title page is decorative, never load-bearing content, so
    this never raises.

    Args:
        doc: The parsed EPUB document.

    Returns:
        The title page as an InputChapter, or None if the book declares none
        or its file is missing from the archive.
    """
    item = title_page_item(doc.opf)
    if item is None:
        return None
    member_name = _member(doc, item.href)
    data = doc.member_bytes(member_name)
    if data is None:
        return None
    labels = {point.src: resolve_nav_title(doc, point) for point in doc.ncx.nav_points()}
    label = _chapter_label(doc, labels, item)
    role = str(ChapterRole.TITLE)
    return InputChapter(label, item.href, item.id, item.media_type, data, role)


def _read_resources(doc: EpubDocument, path: Path, chapter_hrefs: list[str]) -> list[InputResource]:
    """Read non-chapter manifest items into InputResource list.

    Excludes NCX documents, EPUB3 nav documents, and XHTML chapters already
    in the chapters list. Logs a warning (but does not raise) for missing
    resource files.

    Args:
        doc: The parsed EPUB document.
        path: The filesystem path (used for error/warning messages).
        chapter_hrefs: List of chapter hrefs to exclude from resources.

    Returns:
        List of InputResource objects.
    """
    chapter_href_set = set(chapter_hrefs)
    resources: list[InputResource] = []

    for item in doc.opf.manifest():
        # Skip chapters
        if item.href in chapter_href_set:
            continue
        # Skip NCX
        if item.media_type == "application/x-dtbncx+xml":
            continue
        # Skip EPUB3 nav document
        if "nav" in item.properties.split():
            continue

        member_name = _member(doc, item.href)
        data = doc.member_bytes(member_name)
        if data is None:
            logger.warning('Merge input "%s": skipping missing resource "%s"', path.name, item.href)
            continue

        resources.append(InputResource(item.href, item.media_type, data))

    return resources


def _member(doc: EpubDocument, href: str) -> str:
    """Resolve a manifest href to an archive member name (``TXE-D8``).

    Args:
        doc: The EPUB document.
        href: The OPF-relative href from the manifest.

    Returns:
        The archive member name (archive-absolute, not OPF-relative); percent-decoded
        when the literal name is absent but its decoded form exists.
    """
    return doc.member_name(href)


def _content_root(hrefs: list[str]) -> str:
    """Compute the longest common directory prefix of chapter hrefs.

    Args:
        hrefs: List of chapter hrefs (OPF-relative).

    Returns:
        The common directory prefix as a POSIX path with no trailing slash,
        or empty string if chapters are at the OPF directory or list is empty.
    """
    if not hrefs:
        return ""
    root = posixpath.dirname(posixpath.commonprefix(hrefs))
    return root
