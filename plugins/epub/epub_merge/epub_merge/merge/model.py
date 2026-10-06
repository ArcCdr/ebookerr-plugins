"""EPUB merge neutral data model and path constants.

The module defines the data structures that flow through the merge pipeline,
from reading source books through planning the output to rendering the merged
result, independent of how input books are read or written. Sibling modules
in ``merge/`` (metadata, chapters, cover, plan, chapter_urls, render, merge,
landmarks, links, reader, assets, pages, and styles) all import from this
module to exchange data between pipeline stages.

The constants ``OPF_DIR``, ``OPF_PATH``, ``NCX_HREF``, and ``NAV_HREF``
name standard locations and file references within an EPUB: the Open
Publication Structure directory ("OEBPS"), the package document path, the
EPUB 2.0 table of contents file, and the EPUB 3.0 navigation document.

The frozen dataclasses define: ``MergeOptions`` (caller's merge operation
configuration), ``MergeOutcome`` (result of a completed merge), ``InputChapter``
and ``InputResource`` (read-in chapter and resource data from a source book),
``InputBook`` (complete metadata and content of a source book), and
``PlannedChapter`` (a chapter in the merged book's planned output structure).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

OPF_DIR = "OEBPS"
OPF_PATH = "OEBPS/content.opf"
NCX_HREF = "toc.ncx"
NAV_HREF = "nav.xhtml"


@dataclass(frozen=True, slots=True)
class MergeOptions:
    """Configuration options for an EPUB merge operation.

    Attributes:
        rewrite_title_page: Whether to rewrite the title page.
        rewrite_book_title: Whether to rewrite the book title.
        cover_image: Optional path to a cover image file.
        description: Optional description text.
        source: Optional source identifier.
        rights: Optional rights/license information.
        publisher: Optional publisher name.
        language: Optional language code.
    """

    rewrite_title_page: bool = False
    rewrite_book_title: bool = False
    cover_image: Path | None = None
    description: str | None = None
    source: str | None = None
    rights: str | None = None
    publisher: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """The result of an EPUB merge operation (EXP-205).

    Attributes:
        chapter_count: The merged survivor's packaged chapter count.
        title: When ``options.rewrite_book_title`` produced a rename, the title
            the merge wrote — never a title read back from the file (EXP-205);
            ``None`` otherwise.
        duplicate_numbers: Chapter-number tokens that appear on more than one chapter
            of the merged result, sorted. A merge never refuses over these (``R17``):
            two books that overlap are still merged, because refusing leaves the user
            with two books and no remedy. The overlap is reported instead — on the
            outcome, in the log, and to the user through the plugin's alert.
    """

    chapter_count: int
    title: str | None = None
    duplicate_numbers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InputChapter:
    """A single chapter from an input EPUB.

    Attributes:
        label: Human-readable chapter label.
        href: OPF-relative href exactly as written in the input manifest.
        item_id: Unique identifier for this chapter in the manifest.
        media_type: MIME type of the chapter document.
        xhtml: Raw bytes of the chapter XHTML content.
        role: Editorial role from classify_spine (TITLE, FRONT, CONTENT, BACK, or OTHER).
    """

    label: str
    href: str
    item_id: str
    media_type: str
    xhtml: bytes
    role: str = ""


@dataclass(frozen=True, slots=True)
class InputResource:
    """A non-chapter resource from an input EPUB (e.g. stylesheet).

    Attributes:
        href: OPF-relative href exactly as written in the input manifest.
        media_type: MIME type of the resource.
        data: Raw bytes of the resource content.
    """

    href: str
    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class InputBook:
    """Complete metadata and content for an input EPUB.

    Attributes:
        index: Book index; 0 is the survivor (target).
        name: Source file name, for logs and errors.
        version: EPUB version ("2.0" or "3.0").
        title: Book title from metadata.
        creators: Tuple of (name, role) tuples for authors.
        contributors: Tuple of (name, role) tuples for contributors.
        language: Language code.
        identifier: Unique identifier for this book.
        source: Optional source metadata.
        rights: Optional rights information.
        publisher: Optional publisher name.
        subjects: Tuple of subject/category strings.
        dates: Tuple of (date_string, event) tuples.
        page_direction: Page direction ("ltr" or "rtl").
        content_root: Directory prefix shared by this book's XHTML documents.
        chapters: Tuple of chapters in reading order (title page excluded —
            see ``title_page``).
        resources: Tuple of non-chapter resources.
        stylesheets: Tuple of stylesheet hrefs.
        cover_href: Optional href to the cover image.
        cover_media_type: Optional MIME type of the cover image.
        title_page: This book's own title-page chapter, read independently of
            ``chapters`` (which excludes it, role-based like every other
            chapter classification in this app), so a merge that is not
            regenerating a title page can still carry the survivor's original
            one through unchanged. None if the book declares no title page.
    """

    index: int
    name: str
    version: str
    title: str
    creators: tuple[tuple[str, str | None], ...]
    contributors: tuple[tuple[str, str], ...]
    language: str
    identifier: str
    source: str | None
    rights: str | None
    publisher: str | None
    subjects: tuple[str, ...]
    dates: tuple[tuple[str, str], ...]
    page_direction: str
    content_root: str
    chapters: tuple[InputChapter, ...]
    resources: tuple[InputResource, ...]
    stylesheets: tuple[str, ...]
    cover_href: str | None
    cover_media_type: str | None
    title_page: InputChapter | None = None


@dataclass(frozen=True, slots=True)
class PlannedChapter:
    """A chapter in the planned merged output.

    Attributes:
        label: Human-readable chapter label.
        filename: Flat output filename (e.g. "chapter_174_sample_delta.xhtml").
        item_id: Unique identifier for this chapter (== filename stem).
        number: Optional chapter number.
        book_index: Index of the source book this chapter came from.
        source_href: The input's OPF-relative href it came from.
        xhtml: The bytes to write (possibly rewritten).
    """

    label: str
    filename: str
    item_id: str
    number: int | None
    book_index: int
    source_href: str
    xhtml: bytes
