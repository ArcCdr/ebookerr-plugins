"""Tests for the EPUB merge model and constants."""

from __future__ import annotations

import pytest
from epub_merge.merge.errors import (
    EpubMergeError,
    MergeContentError,
    MergeInputError,
    MergeStructureError,
)
from epub_merge.merge.model import (
    NAV_HREF,
    NCX_HREF,
    OPF_DIR,
    OPF_PATH,
    InputBook,
    InputChapter,
    InputResource,
    MergeOptions,
)


def test_merge_options_defaults() -> None:
    """MergeOptions() with no arguments has all default values."""
    options = MergeOptions()
    assert options.rewrite_title_page is False
    assert options.rewrite_book_title is False
    assert options.cover_image is None
    assert options.description is None
    assert options.source is None
    assert options.rights is None
    assert options.publisher is None
    assert options.language is None


def test_merge_options_is_frozen() -> None:
    """MergeOptions instances are frozen and cannot be modified."""
    options = MergeOptions()
    with pytest.raises(AttributeError):
        options.rewrite_title_page = True


def test_path_constants() -> None:
    """Path constants have expected values."""
    assert OPF_PATH == "OEBPS/content.opf"
    assert OPF_DIR == "OEBPS"
    assert NCX_HREF == "toc.ncx"
    assert NAV_HREF == "nav.xhtml"


def test_error_hierarchy() -> None:
    """Error classes form the expected hierarchy."""
    assert issubclass(MergeInputError, EpubMergeError)
    assert issubclass(MergeContentError, EpubMergeError)
    assert issubclass(MergeStructureError, EpubMergeError)
    assert issubclass(EpubMergeError, Exception)


def test_input_book_is_constructible() -> None:
    """InputBook can be constructed with the expected fields."""
    chapter = InputChapter(
        label="Chapter 1",
        href="OEBPS/ch01.xhtml",
        item_id="ch01",
        media_type="application/xhtml+xml",
        xhtml=b"<html></html>",
    )
    resource = InputResource(
        href="OEBPS/stylesheet.css",
        media_type="text/css",
        data=b"body { color: black; }",
    )
    book = InputBook(
        index=0,
        name="book.epub",
        version="3.0",
        title="Test Book",
        creators=(("Author Name", "aut"),),
        contributors=(("Editor Name", "edt"),),
        language="en",
        identifier="test-id-123",
        source="https://example.com",
        rights="© 2024",
        publisher="Test Publisher",
        subjects=("Fiction", "Adventure"),
        dates=(("2024-01-01", "publication"),),
        page_direction="ltr",
        content_root="OEBPS/",
        chapters=(chapter,),
        resources=(resource,),
        stylesheets=("OEBPS/stylesheet.css",),
        cover_href="OEBPS/cover.jpg",
        cover_media_type="image/jpeg",
    )
    assert book.index == 0
    assert book.name == "book.epub"
    assert book.version == "3.0"
    assert book.title == "Test Book"
    assert book.language == "en"
    assert len(book.chapters) == 1
    assert book.chapters[0].label == "Chapter 1"
    assert len(book.resources) == 1
    assert book.resources[0].media_type == "text/css"
