"""Shared fixtures for the staged first-party plugins' tests (``PMG-D26``).

Plugin tests never import ``src`` or ``tests`` (gate
``tests/unit/test_plugin_boundary.py``), so the EPUB builders they need are a
verbatim copy of the ones in ``tests/conftest.py`` (the core keeps its own copy;
``tests/unit/test_plugins_conftest.py`` fails when the two drift). Importing this
module also puts every staged plugin folder on ``sys.path``, so a test imports its
plugin package by name (``from epub_merge.plugin import EpubMergePlugin``) exactly
as the plugin's ``entrypoint.py`` does.
"""

from __future__ import annotations

import sys
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

PLUGINS_ROOT = Path(__file__).resolve().parent
EpubChapter = tuple[str, str]


def plugin_package_dirs(root: Path) -> list[Path]:
    """Return the staged plugin folders under *root*: direct children holding a ``manifest.toml``.

    Args:
        root: The folder of plugin folders (``plugins/``).

    Returns:
        The plugin folders, sorted by name.
    """
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "manifest.toml").is_file())


for _folder in plugin_package_dirs(PLUGINS_ROOT):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))


_CONTAINER_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "\t<rootfiles>\n"
    '\t\t<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>\n'
    "\t</rootfiles>\n</container>\n"
)


def _attr(value: str) -> str:
    """Escape *value* for use inside an XML attribute."""
    return escape(value, {'"': "&quot;"})


def _navpoint(nav_id: str, play_order: int, label: str, src: str) -> str:
    """Render one NCX ``navPoint`` element."""
    return (
        f'\t\t<navPoint id="{nav_id}" playOrder="{play_order}">\n'
        f"\t\t\t<navLabel>\n\t\t\t\t<text>{escape(label)}</text>\n\t\t\t</navLabel>\n"
        f'\t\t\t<content src="{_attr(src)}"/>\n\t\t</navPoint>'
    )


def _xhtml_chapter(label: str, url: str) -> str:
    """Render one FanFicFare-shaped XHTML chapter that carries its source URL."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        f"<title>{escape(label)}</title>\n"
        '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
        f'<meta name="chapterurl" content="{_attr(url)}" />\n'
        f'<meta name="chaptertitle" content="{_attr(label)}" />\n'
        '</head>\n<body class="fff_chapter">\n'
        f'<h3 class="fff_chapter_title">{escape(label)}</h3>\n'
        "<p>Body.</p>\n</body>\n</html>\n"
    )


def _xhtml_titlepage(doc_title: str) -> str:
    """Render the FanFicFare-shaped title page."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        f"<title>{escape(doc_title)}</title>\n"
        '<link href="stylesheet.css" type="text/css" rel="stylesheet"/>\n'
        '</head>\n<body class="fff_titlepage">\n'
        f"<h3>{escape(doc_title)}</h3>\n</body>\n</html>\n"
    )


def _opf(
    doc_title: str,
    author: str,
    manifest: list[str],
    spine: list[str],
    extra_meta: str = "",
    language: str | None = "en",
    source: str | None = None,
    identifier: str | None = "fanficfare-uid:test",
) -> str:
    """Render the OPF package document for :func:`_build_epub`."""
    items = "".join(f"\t\t{line}\n" for line in manifest)
    refs = "".join(f"\t\t{line}\n" for line in spine)
    lang_line = f"\t\t<dc:language>{escape(language)}</dc:language>\n" if language else ""
    source_line = f"\t\t<dc:source>{escape(source)}</dc:source>\n" if source else ""
    identifier_line = (
        f'\t\t<dc:identifier id="fanficfare-uid">{escape(identifier)}</dc:identifier>\n'
        if identifier
        else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package version="2.0" xmlns="http://www.idpf.org/2007/opf" '
        'unique-identifier="fanficfare-uid">\n'
        '\t<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">\n'
        f"{identifier_line}"
        f'\t\t<dc:title id="id">{escape(doc_title)}</dc:title>\n'
        f'\t\t<dc:creator opf:role="aut">{escape(author)}</dc:creator>\n'
        f"{lang_line}"
        f"{source_line}"
        '\t\t<dc:date opf:event="modification">2026-01-01</dc:date>\n'
        f"{extra_meta}"
        f"\t</metadata>\n\t<manifest>\n{items}\t</manifest>\n"
        f'\t<spine toc="ncx">\n{refs}\t</spine>\n</package>\n'
    )


def _ncx(doc_title: str, navpoints: list[str]) -> str:
    """Render the NCX table of contents for :func:`_build_epub`."""
    points = "\n".join(navpoints)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">\n'
        '\t<head>\n\t\t<meta name="dtb:uid" content="fanficfare-uid:test"/>\n'
        '\t\t<meta name="dtb:depth" content="1"/>\n'
        '\t\t<meta name="dtb:totalPageCount" content="0"/>\n'
        '\t\t<meta name="dtb:maxPageNumber" content="0"/>\n\t</head>\n'
        f"\t<docTitle>\n\t\t<text>{escape(doc_title)}</text>\n\t</docTitle>\n"
        f"\t<navMap>\n{points}\n\t</navMap>\n</ncx>\n"
    )


def _build_epub(
    path: Path,
    chapters: Sequence[EpubChapter],
    *,
    doc_title: str = "Sample Book",
    author: str = "Author",
    include_title_page: bool = True,
    cover: bytes | None = None,
    description: str | None = None,
    language: str | None = "en",
    source: str | None = None,
    identifier: str | None = "fanficfare-uid:test",
) -> Path:
    """Write a FanFicFare-shaped EPUB (root content.opf/toc.ncx, OEBPS chapters).

    navMap/spine follow ``chapters`` order (so callers can build out-of-order
    books); ``playOrder`` is 0-based with the title page first, mirroring the real
    sample (``books/Moosetales/Tending Bar.epub``).
    """
    ids = [f"file{i:04d}" for i in range(1, len(chapters) + 1)]
    manifest = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="style" href="OEBPS/stylesheet.css" media-type="text/css"/>',
    ]
    spine: list[str] = []
    navpoints: list[str] = []
    members: dict[str, str | bytes] = {}
    order: list[str] = ["META-INF/container.xml", "OEBPS/stylesheet.css"]
    extra_meta = ""
    if cover is not None:
        members["OEBPS/cover.jpg"] = cover
        order.append("OEBPS/cover.jpg")
        manifest.append('<item id="coverimage" href="OEBPS/cover.jpg" media-type="image/jpeg"/>')
        extra_meta = '\t\t<meta name="cover" content="coverimage"/>\n'
    if description is not None:
        extra_meta += f"\t\t<dc:description>{escape(description)}</dc:description>\n"

    play_order = 0
    if include_title_page:
        manifest.append(
            '<item id="title_page" href="OEBPS/title_page.xhtml" '
            'media-type="application/xhtml+xml"/>'
        )
        spine.append('<itemref idref="title_page" linear="yes"/>')
        navpoints.append(
            _navpoint("title_page", play_order, "Title Page", "OEBPS/title_page.xhtml")
        )
        members["OEBPS/title_page.xhtml"] = _xhtml_titlepage(doc_title)
        order.append("OEBPS/title_page.xhtml")
        play_order += 1

    for cid, (label, url) in zip(ids, chapters, strict=True):
        href = f"OEBPS/{cid}.xhtml"
        manifest.append(f'<item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{cid}" linear="yes"/>')
        navpoints.append(_navpoint(cid, play_order, label, href))
        members[href] = _xhtml_chapter(label, url)
        order.append(href)
        play_order += 1

    members["META-INF/container.xml"] = _CONTAINER_XML
    members["OEBPS/stylesheet.css"] = "body { margin: 2%; }\n"
    members["content.opf"] = _opf(
        doc_title, author, manifest, spine, extra_meta, language, source, identifier
    )
    members["toc.ncx"] = _ncx(doc_title, navpoints)
    order += ["content.opf", "toc.ncx"]

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        for name in order:
            archive.writestr(name, members[name], zipfile.ZIP_DEFLATED)
    return path


@pytest.fixture
def build_epub(tmp_path: Path) -> Callable[..., Path]:
    """Factory: build a FanFicFare-shaped EPUB under ``tmp_path`` (see ``_build_epub``)."""

    def _factory(
        chapters: Sequence[EpubChapter], *, filename: str = "book.epub", **kw: object
    ) -> Path:
        """Call _build_epub with tmp_path and return the path."""
        return _build_epub(tmp_path / filename, chapters, **kw)  # type: ignore[arg-type]

    return _factory


def _nav_xhtml(navpoints: list[str]) -> str:
    """Render an EPUB 3 ``nav`` document."""
    items = "".join(f"\t\t\t{line}\n" for line in navpoints)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '\t<head>\n\t\t<meta charset="utf-8"/>\n\t</head>\n'
        '\t<body>\n\t\t<nav epub:type="toc" id="toc">\n\t\t\t<ol>\n'
        f"{items}"
        "\t\t\t</ol>\n\t\t</nav>\n\t</body>\n</html>\n"
    )


def _nav_li(nav_id: str, label: str, href: str) -> str:
    """Render one EPUB 3 ``nav`` list item."""
    return f'<li id="{_attr(nav_id)}"><a href="{_attr(href)}">{escape(label)}</a></li>'


def _nav_opf(doc_title: str, author: str, manifest: list[str], spine: list[str]) -> str:
    """Render the OPF package document for :func:`_build_nav_epub`."""
    items = "".join(f"\t\t{line}\n" for line in manifest)
    refs = "".join(f"\t\t{line}\n" for line in spine)
    creator = f"\t\t<dc:creator>{escape(author)}</dc:creator>\n" if author else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" '
        'unique-identifier="uid">\n'
        '\t<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '\t\t<dc:identifier id="uid">nav-uid:test</dc:identifier>\n'
        "\t\t<dc:language>en</dc:language>\n"
        f"\t\t<dc:title>{escape(doc_title)}</dc:title>\n"
        f"{creator}"
        f"\t</metadata>\n\t<manifest>\n{items}\t</manifest>\n"
        f"\t<spine>\n{refs}\t</spine>\n</package>\n"
    )


def _nav_xhtml_chapter(label: str) -> str:
    """Render one EPUB 3 XHTML chapter."""
    # class="fff_chapter" matches _xhtml_chapter's convention above, keeping the
    # two builders' output interchangeable for tests that exercise cross-backend
    # chapter operations.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        f"<title>{escape(label)}</title>\n"
        '</head>\n<body class="fff_chapter">\n'
        f"<h1>{escape(label)}</h1>\n<p>Body.</p>\n</body>\n</html>\n"
    )


def _build_nav_epub(
    path: Path,
    chapters: Sequence[EpubChapter],
    *,
    doc_title: str = "Sample Book",
    author: str = "Author",
) -> Path:
    """Write an EPUB3 nav-only book (no toc.ncx).

    The shape Patreon's Google-Docs-exported story attachments use:
    ``<item properties="nav">`` in the manifest, navigation entirely in
    ``nav.xhtml``.
    """
    ids = [f"chap{i:04d}" for i in range(1, len(chapters) + 1)]
    manifest = [
        '<item id="toc" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    ]
    spine: list[str] = []
    navpoints: list[str] = []
    members: dict[str, str] = {}
    order: list[str] = ["META-INF/container.xml"]

    for cid, (label, _url) in zip(ids, chapters, strict=True):
        href = f"{cid}.xhtml"
        manifest.append(f'<item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{cid}"/>')
        navpoints.append(_nav_li(cid, label, href))
        members[href] = _nav_xhtml_chapter(label)
        order.append(href)

    members["META-INF/container.xml"] = _CONTAINER_XML
    members["content.opf"] = _nav_opf(doc_title, author, manifest, spine)
    members["nav.xhtml"] = _nav_xhtml(navpoints)
    order += ["content.opf", "nav.xhtml"]

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        for name in order:
            archive.writestr(name, members[name], zipfile.ZIP_DEFLATED)
    return path


@pytest.fixture
def build_nav_epub(tmp_path: Path) -> Callable[..., Path]:
    """Factory: build an EPUB3 nav-only book under ``tmp_path`` (see ``_build_nav_epub``)."""

    def _factory(
        chapters: Sequence[EpubChapter], *, filename: str = "book.epub", **kw: object
    ) -> Path:
        """Call _build_nav_epub with tmp_path and return the path."""
        return _build_nav_epub(tmp_path / filename, chapters, **kw)  # type: ignore[arg-type]

    return _factory


@pytest.fixture
def epub_fixtures(request: pytest.FixtureRequest) -> Path:
    """The calling test file's own fixture folder, ``plugins/<id>/tests/fixtures/``.

    Args:
        request: The pytest request of the test asking for it.

    Returns:
        ``<folder of the test file>/fixtures``.
    """
    return Path(request.path).resolve().parent / "fixtures"
