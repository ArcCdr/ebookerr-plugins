"""Tests for lossless encoding handling in the EPUB normalize orchestrator (``TXE-D8``)."""

from __future__ import annotations

import logging

from ebookerr_sdk.epub.archive import EpubArchive
from ebookerr_sdk.epub.document import EpubDocument
from epub_normalize.normalize import NormalizeOptions, normalize_epub


def test_a_latin1_chapter_keeps_its_accents_and_is_redeclared_utf8(build_epub) -> None:
    """A chapter declared ISO-8859-1 is normalized, keeps its accents, and is redeclared UTF-8."""
    path = build_epub(
        [("Chapter 1", "https://example.test/1")],
        include_title_page=False,
    )
    chapter_xhtml = (
        '<?xml version="1.0" encoding="iso-8859-1"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        "<title>Chapter 1</title>\n"
        "<style>p { font-family: Georgia; margin: 0 }</style>\n"
        '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
        '<meta name="chapterurl" content="https://example.test/1" />\n'
        '<meta name="chaptertitle" content="Chapter 1" />\n'
        '</head>\n<body class="fff_chapter">\n'
        '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
        "<p>café Body.</p>\n</body>\n</html>\n"
    )
    archive = EpubArchive.open(path)
    archive.write_bytes("OEBPS/file0001.xhtml", chapter_xhtml.encode("iso-8859-1"))
    archive.save()

    normalize_epub(path, options=NormalizeOptions())

    doc = EpubDocument.open(path)
    raw = doc.resource_bytes("OEBPS/file0001.xhtml")
    chapter = raw.decode("utf-8")

    assert "café" in chapter
    assert 'encoding="utf-8"' in chapter
    assert chr(0xFFFD) not in chapter


def test_an_undecodable_chapter_is_left_byte_identical_with_one_warning(build_epub, caplog) -> None:
    """A chapter that cannot decode in its declared encoding is left completely untouched."""
    path = build_epub(
        [("Chapter 1", "https://example.test/1")],
        include_title_page=False,
    )
    chapter_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        "<title>Chapter 1</title>\n"
        "<style>p { font-family: Georgia; margin: 0 }</style>\n"
        '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
        '<meta name="chapterurl" content="https://example.test/1" />\n'
        '<meta name="chaptertitle" content="Chapter 1" />\n'
        '</head>\n<body class="fff_chapter">\n'
        '<h3 class="fff_chapter_title">Chapter 1</h3>\n'
        "<p>Body.</p>\n</body>\n</html>\n"
    )
    corrupted = chapter_xhtml.encode("utf-8").replace(b"Body.", b"Bod\xffy.")

    archive = EpubArchive.open(path)
    archive.write_bytes("OEBPS/file0001.xhtml", corrupted)
    archive.save()

    with caplog.at_level(logging.WARNING):
        normalize_epub(path, options=NormalizeOptions())

    doc = EpubDocument.open(path)
    assert doc.resource_bytes("OEBPS/file0001.xhtml") == corrupted

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("Normalize skipped ")


def test_a_charset_stylesheet_keeps_the_rule_first(build_epub) -> None:
    """A stylesheet's leading @charset rule stays first; the marker is inserted right after it."""
    path = build_epub(
        [("Chapter 1", "https://example.test/1")],
        include_title_page=False,
    )
    css_text = (
        '@charset "iso-8859-1";\n/* café */\nbody { font-family: Georgia; color: #222; margin: 2% }'
    )
    archive = EpubArchive.open(path)
    archive.write_bytes("OEBPS/stylesheet.css", css_text.encode("iso-8859-1"))
    archive.save()

    normalize_epub(path, options=NormalizeOptions())

    doc = EpubDocument.open(path)
    stylesheet = doc.resource_bytes("OEBPS/stylesheet.css").decode("utf-8")

    assert stylesheet.startswith('@charset "utf-8";\n')
    remainder = stylesheet.split('@charset "utf-8";\n', 1)[1]
    assert remainder.startswith("/* ebookerr-normalized:")
    assert "café" in stylesheet
