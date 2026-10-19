"""Acceptance tests for EPUB Normalize on real shapes, driven through the plugin.

Each test runs ``EpubNormalizePlugin().process`` with a level, exactly as the core does, on a
packaged fixture or a built book carrying one of the shapes the scope names: dark-mode
colours, Google Docs sizes and blank lines, dead fonts, drop caps, idempotency, paragraph
separation, FanFicFare's stylesheet and a book already normalised by 1.x. The module imports
only names the 1.x engine also has, so the same file can be run against it as a red proof.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
import tinycss2
from ebookerr_sdk.epub import EpubDocument
from ebookerr_sdk.testing import FakeContext, make_epub_item
from epub_normalize.normalize import NormalizeOptions, options_signature
from epub_normalize.plugin import EpubNormalizePlugin

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_FIXTURE_NAMES = sorted(p.name for p in FIXTURES.glob("*.epub"))
_LEVELS = ("light", "standard", "strict")
_CHAPTER = "OEBPS/file0001.xhtml"
_STYLESHEET = "OEBPS/stylesheet.css"


def _copy(name: str, tmp_path: Path) -> Path:
    """Copy fixture *name* into *tmp_path* and return the copy."""
    target = tmp_path / name
    shutil.copy2(FIXTURES / name, target)
    return target


def _normalise(path: Path, level: str) -> None:
    """Run EPUB Normalize on *path* at *level* through the plugin, as the core does."""
    item = make_epub_item(path, book_id="b1", title="Test Book")
    EpubNormalizePlugin().process((item,), FakeContext(settings={"level": level}))


def _all_text(path: Path) -> str:
    """Every ``.css``/``.xhtml``/``.html`` member of *path*, decoded and joined."""
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith((".css", ".xhtml", ".html"))]
        return "\n".join(archive.read(n).decode("utf-8", "replace") for n in names)


def _member(path: Path, name: str) -> str:
    """Return the archive member *name* of *path*, decoded."""
    with zipfile.ZipFile(path) as archive:
        return archive.read(name).decode("utf-8")


def _style_blocks(xhtml: str) -> str:
    """Return the text of every ``<style>`` element in *xhtml*, joined."""
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", xhtml, flags=re.DOTALL))


def declarations_of(css: str) -> dict[str, dict[str, str]]:
    """Map each top-level rule's selector text to its {property: value}, whitespace-stripped."""
    out: dict[str, dict[str, str]] = {}
    for rule in tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True):
        if rule.type != "qualified-rule":
            continue
        decls = tinycss2.parse_blocks_contents(
            rule.content, skip_comments=True, skip_whitespace=True
        )
        out[tinycss2.serialize(rule.prelude).strip()] = {
            d.lower_name: tinycss2.serialize(d.value).strip()
            for d in decls
            if d.type == "declaration"
        }
    return out


def _set_member(path: Path, href: str, text: str | bytes) -> None:
    """Write *text* into the resource *href* of the book at *path*."""
    doc = EpubDocument.open(path)
    doc.write_resource(href, text.encode("utf-8") if isinstance(text, str) else text)
    doc.save()


def _chapter_xhtml(head: str, body: str) -> str:
    """Return a minimal XHTML chapter with *head* extras and the *body* markup."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n<title>T</title>\n'
        f"{head}</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def test_ac1_light_leaves_no_colour(
    build_epub: Callable[..., Path],
) -> None:
    """Light removes every colour declaration and attribute, keeping the border's shape."""
    path = build_epub([("One", "u")])
    _set_member(
        path,
        _STYLESHEET,
        "body { color: #000; background-color: #fff } "
        "p { color: black; border: 1px solid #000 } "
        ".x { -webkit-text-fill-color: #111 }",
    )
    _set_member(
        path,
        _CHAPTER,
        _chapter_xhtml(
            '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n',
            '<p><font color="#000000">a</font></p>'
            '<table bgcolor="#ffffff"><tr><td>b</td></tr></table>',
        ),
    )

    _normalise(path, "light")

    text = _all_text(path).lower()
    for forbidden in (
        "color:",
        "background-color",
        "-webkit-text-fill-color",
        "#000",
        "color=",
        "bgcolor=",
    ):
        assert forbidden not in text, forbidden
    assert "1px solid" in text


def test_ac2_google_docs_sizes_follow_the_reader(tmp_path: Path) -> None:
    """Standard drops the 11pt base and converts the heading to a ratio; blanks are filled."""
    path = _copy("Gamma35.epub", tmp_path)

    _normalise(path, "standard")

    chapter = _member(path, "GoogleDoc/Gamma35.xhtml")
    rules = declarations_of(_style_blocks(chapter))
    assert "font-size" not in rules.get("p", {})
    assert "font-size" not in rules.get(".c1", {})
    assert rules["h1"]["font-size"] == "1.818em"
    assert "height" not in rules.get(".c0", {})
    assert "line-height" not in rules.get(".c0", {})
    assert chapter.count("&#160;</p>") == 1401


def test_ac4_dead_fonts_removed_at_light(build_epub: Callable[..., Path]) -> None:
    """A font only an ``@font-face`` references goes, with its manifest item and the rule."""
    path = build_epub([("One", "u")])
    doc = EpubDocument.open(path)
    doc.opf.add_manifest_item("deadfont", "OEBPS/fonts/f.ttf", "font/ttf")
    doc.write_resource("OEBPS/fonts/f.ttf", b"\0" * 2_000_000)
    doc.write_resource(
        _STYLESHEET, b"@font-face { font-family: F; src: url(fonts/f.ttf) }\np { margin: 0 }\n"
    )
    doc.save()
    assert "OEBPS/fonts/f.ttf" in zipfile.ZipFile(path).namelist()

    _normalise(path, "light")

    reopened = EpubDocument.open(path)
    assert reopened.opf.item_by_id("deadfont") is None
    assert "OEBPS/fonts/f.ttf" not in zipfile.ZipFile(path).namelist()
    assert "@font-face" not in _member(path, _STYLESHEET)


_DROP_CAP_CSS = (
    "p.first::first-letter { font-size: 300%; color: red; float: left; } "
    "p.first { color: #000; text-indent: 0; font-family: Georgia }"
)


def test_ac5_drop_cap_fidelity(build_epub: Callable[..., Path], tmp_path: Path) -> None:
    """Standard keeps a drop cap's size and float; Strict removes the drop cap."""
    standard = build_epub([("One", "u")], filename="standard.epub")
    strict = build_epub([("One", "u")], filename="strict.epub")
    for path in (standard, strict):
        _set_member(path, _STYLESHEET, _DROP_CAP_CSS)

    _normalise(standard, "standard")
    _normalise(strict, "strict")

    assert declarations_of(_member(standard, _STYLESHEET)) == {
        "p.first::first-letter": {"font-size": "300%", "float": "left"},
        "p.first": {"text-indent": "0"},
    }
    assert declarations_of(_member(strict, _STYLESHEET)) == {"p.first": {"text-indent": "0"}}


@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_ac6_second_run_is_byte_identical(name: str, level: str, tmp_path: Path) -> None:
    """A second run at the same level leaves the file's bytes untouched."""
    path = _copy(name, tmp_path)

    _normalise(path, level)
    first = hashlib.sha256(path.read_bytes()).hexdigest()
    _normalise(path, level)
    second = hashlib.sha256(path.read_bytes()).hexdigest()

    assert first == second


@pytest.mark.parametrize("level", _LEVELS)
def test_paragraph_separation_survives_every_level(
    level: str, build_epub: Callable[..., Path]
) -> None:
    """Paragraphs, breaks, rules, pre-wrap and indent keep separating the text at any level."""
    path = build_epub([("One", "u")])
    _set_member(path, _STYLESHEET, "p { margin: 0; text-indent: 1.5em; display: block }")
    body = (
        "<p>One</p><p></p><p>Two<br/>Three</p>"
        '<p style="white-space: pre-wrap">Four\nFive</p><hr/><p>Six</p>'
    )
    _set_member(
        path,
        _CHAPTER,
        _chapter_xhtml('<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n', body),
    )

    _normalise(path, level)

    chapter = _member(path, _CHAPTER)
    for tag in ("p", "br", "hr"):
        assert len(re.findall(rf"<{tag}[\s>/]", chapter)) == len(
            re.findall(rf"<{tag}[\s>/]", body)
        ), tag
    assert re.search(r"white-space:\s*pre-wrap", chapter)
    rule = declarations_of(_member(path, _STYLESHEET))["p"]
    assert rule["text-indent"] == "1.5em"
    assert rule["margin"] == "0"
    assert re.search(r"<p[^>]*>(?:&#160;| )</p>", chapter)


def _fanficfare_at(level: str, tmp_path: Path) -> Path:
    """Return a copy of ``sample_alpha.epub`` normalised at *level*, in its own folder."""
    folder = tmp_path / level
    folder.mkdir()
    path = _copy("sample_alpha.epub", folder)
    _normalise(path, level)
    return path


def test_fanficfare_css_hands_the_page_to_the_reader(tmp_path: Path) -> None:
    """FanFicFare's ``body`` rule goes at Standard, keeps its layout at Light."""
    books = {level: _fanficfare_at(level, tmp_path) for level in _LEVELS}

    rules = declarations_of(_member(books["standard"], _STYLESHEET))
    assert "body" not in rules
    assert rules[".CI"] == {
        "text-align": "center",
        "margin-top": "0",
        "margin-bottom": "0",
        "padding": "0",
    }
    assert rules["h1"]["text-align"] == "center"
    assert declarations_of(_member(books["light"], _STYLESHEET))["body"] == {
        "text-align": "justify",
        "margin": "2%",
        "adobe-hyphenate": "none",
    }
    for book in books.values():
        assert "<b>Summary:</b> <p></p>" in _member(book, "OEBPS/title_page.xhtml")


def test_a_book_normalised_by_1_x_is_repaired(build_epub: Callable[..., Path]) -> None:
    """A 1.x marker and its fractional sizes are redone, and empty paragraphs shown."""
    path = build_epub([("One", "u")])
    head = (
        "<style>p{margin:0;font-size:0.917em;}.c0{font-weight:400;font-size:0.917em;}"
        "h1{font-size:1.667em;}</style>\n<!--ebookerr-normalized:74ddf969e42f-->\n"
    )
    body = '<p class="c1"><span class="c0">t</span></p>' * 20 + (
        '<p class="c1 c2"><span class="c0"/></p>' * 20
    )
    _set_member(path, _CHAPTER, _chapter_xhtml(head, body))

    _normalise(path, "standard")

    chapter = _member(path, _CHAPTER)
    rules = declarations_of(_style_blocks(chapter))
    assert "font-size" not in rules.get("p", {})
    assert "font-size" not in rules.get(".c0", {})
    assert rules["h1"]["font-size"] == "1.667em"
    assert chapter.count("&#160;</span></p>") + chapter.count("&#160;</p>") == 20
    assert f"ebookerr-normalized:{options_signature(NormalizeOptions())}" in chapter
    assert "74ddf969e42f" not in chapter
