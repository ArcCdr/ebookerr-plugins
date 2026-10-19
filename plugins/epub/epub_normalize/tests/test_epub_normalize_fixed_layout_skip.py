"""A book EPUB Normalize does not change is reported to the run: a skip or a failure (EXP-395)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.epub.opf import PackageDocument
from ebookerr_sdk.testing import FakeContext, make_epub_item
from epub_normalize.plugin import EpubNormalizePlugin


def _make_fixed_layout(epub: Path) -> None:
    """Mark *epub* as a fixed-layout (pre-paginated) book."""
    doc = EpubDocument.open(epub)
    opf_xml = doc.opf.to_xml().replace(
        "</metadata>", '<meta property="rendition:layout">pre-paginated</meta></metadata>'
    )
    doc.opf = PackageDocument.parse(opf_xml)
    doc.save()


@pytest.mark.pins("EXP-395")
def test_a_fixed_layout_book_is_reported_as_skipped(build_epub: Callable[..., Path]) -> None:
    """The run reports a fixed-layout book as a skip with its reason and leaves the file as is."""
    epub = build_epub([("Chapter One", "Test content")])
    _make_fixed_layout(epub)
    before = epub.read_bytes()
    ctx = FakeContext()
    assert EpubNormalizePlugin().process((make_epub_item(epub, book_id="b1"),), ctx) == []
    assert ctx.skips == [("b1", "fixed-layout book")]
    assert ctx.failures == []
    assert epub.read_bytes() == before


_APPLE_FIXED_LAYOUT = (
    b'<?xml version="1.0" encoding="UTF-8"?><display_options><platform name="*">'
    b'<option name="fixed-layout">true</option></platform></display_options>'
)


def test_a_book_apple_marks_fixed_layout_is_reported_as_skipped(
    build_epub: Callable[..., Path],
) -> None:
    """Apple's display-options file declaring fixed-layout true skips the book untouched."""
    epub = build_epub([("Chapter One", "Test content")])
    doc = EpubDocument.open(epub)
    doc.write_member("META-INF/com.apple.ibooks.display-options.xml", _APPLE_FIXED_LAYOUT)
    doc.save()
    before = epub.read_bytes()
    ctx = FakeContext()
    EpubNormalizePlugin().process((make_epub_item(epub, book_id="b1"),), ctx)
    assert ctx.skips == [("b1", "fixed-layout book")]
    assert ctx.failures == []
    assert epub.read_bytes() == before


def test_a_drm_protected_book_is_reported_as_skipped(
    build_epub: Callable[..., Path], caplog: pytest.LogCaptureFixture
) -> None:
    """A book with a licence file is reported as a skip, warned about and left untouched."""
    epub = build_epub([("Chapter One", "Test content")])
    doc = EpubDocument.open(epub)
    doc.write_member("META-INF/rights.xml", b"<rights/>")
    doc.save()
    before = epub.read_bytes()
    ctx = FakeContext()
    with caplog.at_level(logging.WARNING):
        EpubNormalizePlugin().process((make_epub_item(epub, book_id="b1", title="Test Book"),), ctx)
    assert ctx.skips == [("b1", "DRM-protected book")]
    assert ctx.failures == []
    assert epub.read_bytes() == before
    assert 'Normalize skipped for "Test Book" (book_id=b1): DRM-protected book' in [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]


def test_a_reflowable_book_is_normalised_and_not_reported(build_epub: Callable[..., Path]) -> None:
    """A reflowable book is normalised as before and reported neither as a skip nor a failure."""
    epub = build_epub([("Chapter One", "Test content")])
    ctx = FakeContext()
    EpubNormalizePlugin().process((make_epub_item(epub, book_id="b1"),), ctx)
    assert ctx.skips == []
    assert ctx.failures == []


def test_a_malformed_epub_is_reported_as_a_failure(tmp_path: Path) -> None:
    """A file that cannot be read fails its book: not a skip, and trying again cannot help."""
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a zip")
    ctx = FakeContext()
    EpubNormalizePlugin().process((make_epub_item(broken, book_id="b2"),), ctx)
    assert ctx.failures == [("b2", "the EPUB file could not be read — it may be damaged", False)]
    assert ctx.skips == []
