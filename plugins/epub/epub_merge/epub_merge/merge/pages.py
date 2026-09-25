"""Regenerated title page rendering for merged EPUB (``MR-PARAM-1``).

Owner decision: the regenerated title page carries metadata only — title, authors, subjects,
language. It does not list the chapters: the TOC already provides that and duplicating it is
explicitly unwanted.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import cast
from xml.dom import minidom

from ebookerr_sdk.domain.chapter_number import classify_special_chapter
from ebookerr_sdk.epub.assets import AssetRegistry
from ebookerr_sdk.epub.serialize import element, new_document, pretty_xml

from epub_merge.merge.model import PlannedChapter

logger = logging.getLogger(__name__)

XHTML11_DOCTYPE = (
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
    b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
)


def render_title_page(
    *,
    title: str,
    creators: Sequence[tuple[str, str | None]],
    subjects: Sequence[str],
    language: str,
    stylesheets: Sequence[str],
) -> bytes:
    """Build the regenerated title page: title, authors, subjects, language (``MR-PARAM-1``).

    Generates a valid XHTML 1.1 document with the provided metadata. The document
    includes the title in both <title> and <h1>, author names as paragraphs with
    class="author", and optional sections for subjects and language.

    Args:
        title: The book title to display.
        creators: Sequence of (name, role) tuples for authors.
        subjects: Sequence of subject/category strings.
        language: Language code (e.g., "en-US").
        stylesheets: Sequence of canonical stylesheet hrefs to link.

    Returns:
        UTF-8 encoded bytes of the XHTML document, prefixed with XML declaration
        and XHTML 1.1 DOCTYPE.
    """
    doc = new_document("html")
    root = cast(minidom.Element, doc.documentElement)
    root.setAttribute("xmlns", "http://www.w3.org/1999/xhtml")

    head = element(root, "head")
    element(head, "title", text=title)
    element(head, "meta", attrs={"charset": "utf-8"})

    for stylesheet in stylesheets:
        element(head, "link", attrs={"href": stylesheet, "type": "text/css", "rel": "stylesheet"})

    body = element(root, "body")
    element(body, "h1", text=title)

    for creator_name, _role in creators:
        element(body, "p", attrs={"class": "author"}, text=creator_name)

    if subjects:
        subjects_text = ", ".join(subjects)
        element(body, "p", attrs={"class": "subjects"}, text=subjects_text)

    if language:
        element(body, "p", attrs={"class": "language"}, text=language)

    rendered = pretty_xml(doc)
    # Insert DOCTYPE after XML declaration
    declaration, _, rest = rendered.partition(b"\n")
    return declaration + b"\n" + XHTML11_DOCTYPE + b"\n" + rest


def apply_title_page(
    chapters: Sequence[PlannedChapter],
    *,
    title: str,
    creators: Sequence[tuple[str, str | None]],
    subjects: Sequence[str],
    language: str,
    stylesheets: Sequence[str],
    registry: AssetRegistry,
) -> list[PlannedChapter]:
    """Replace (or insert) the merged book's title page with a regenerated one.

    Finds the first chapter that is a title page (by item_id or classification),
    replaces its xhtml with the regenerated page (keeping filename, item_id, label, position).
    If no title page is found, inserts a new one at index 0.

    Args:
        chapters: Planned chapters in reading order.
        title: The output book title.
        creators: Merged creators from all input books.
        subjects: Merged subjects from all input books.
        language: Resolved output language code.
        stylesheets: Canonical stylesheets to link.
        registry: The asset registry for reserving collision-free paths for the
            synthesised title page.

    Returns:
        A list of PlannedChapter with the title page regenerated or inserted.
    """
    rendered = render_title_page(
        title=title,
        creators=creators,
        subjects=subjects,
        language=language,
        stylesheets=stylesheets,
    )

    # Find existing title page
    for i, chapter in enumerate(chapters):
        if chapter.item_id == "title_page" or classify_special_chapter(chapter.label) == "title":
            # Replace in place
            result = list(chapters)
            result[i] = replace(chapter, xhtml=rendered)
            logger.info('Merge title page regenerated for "%s"', title)
            return result

    # No title page found; insert at index 0
    filename = registry.reserve("title_page.xhtml")
    if filename != "title_page.xhtml":
        logger.debug("Merge title page reserved a free path: %s", filename)

    title_page = PlannedChapter(
        label="Title Page",
        filename=filename,
        item_id="title_page",
        number=None,
        book_index=0,
        source_href="",
        xhtml=rendered,
    )
    result = [title_page] + list(chapters)
    logger.info('Merge title page created for "%s" (the merge set had none)', title)
    return result
