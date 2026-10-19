"""The book survey: body-text size, root-only names and exempt families (TR-NORM-7).

Before any rewrite, the survey reads every stylesheet and chapter of a book once and fixes
what the rewrite passes need to know about the whole book. The body-text size is the font size
that the most elements receive: each rule's font-size is weighed by how many census elements its
selectors address, and the heaviest size is trusted only when it weighs at least half the book's
block elements, so a book with no dominant size is left unscaled. Classes and ids used only on
the root address the page rather than the text, so they are named here for the selector model.
The font families that exempt contexts (vector art and drop caps) use are kept, so stripping
fonts never removes them. Calibre's base-font-size survey is the prior art for the body size;
this one weighs each selector against the book's actual start tags.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

import tinycss2
from ebookerr_sdk.domain.text_encoding import decode_declared
from ebookerr_sdk.epub.document import EpubDocument
from ebookerr_sdk.epub.errors import ItemNotFoundError
from ebookerr_sdk.epub.opf import ManifestItem

from .options import NormalizeOptions
from .selectors import ROOT_TAGS, Compound, Selector, context_of, parse_selector_list
from .units import BASE_FONT_PX, px_value
from .values import FONT_SIZE_KEYWORDS, expand_font_shorthand, family_names

logger = logging.getLogger(__name__)

BLOCK_TAGS: frozenset[str] = frozenset({"p", "div", "li", "blockquote", "dd", "td"})
BASE_SHARE = 0.5
"""The body-text size must weigh at least this share of the book's block count (FR-NORM-7)."""

START_TAG_RE = re.compile(
    r"<([A-Za-z][A-Za-z0-9:_.-]*)"
    r"((?:\s+[^\s=/>]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?)*)"
    r"\s*(/?)>"
)
ATTR_RE = re.compile(r"(\s+)([^\s=/>]+)(?:(\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?")
_CENSUS_MASK_RE = re.compile(
    r"<!--.*?-->|<style\b[^>]*>.*?</style\s*>|<script\b[^>]*>.*?</script\s*>"
    r"|<(?:\w+:)?svg\b[^>]*>.*?</(?:\w+:)?svg\s*>|<(?:\w+:)?math\b[^>]*>.*?</(?:\w+:)?math\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SVG_RE = re.compile(r"<(?:\w+:)?svg\b[^>]*>.*?</(?:\w+:)?svg\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
_XHTML_SUFFIXES = (".xhtml", ".html", ".htm")

_Entry = tuple[str, frozenset[str], str | None]
_Rule = tuple[tuple[Selector, ...], list[Any]]


@dataclass(frozen=True, slots=True)
class FontSize:
    """One ``font-size`` value: a number and a lower-case unit (``"%"`` for a percentage)."""

    value: float
    unit: str

    @property
    def px(self) -> float | None:
        """The size in px for an absolute unit (``units.PX_PER_UNIT``), else ``None``."""
        return px_value(self.value, self.unit)

    def same_as(self, other: FontSize) -> bool:
        """Equal sizes: both absolute and within 0.01 px, or the same unit and value (1e-6).

        Args:
            other: The size to compare with.

        Returns:
            ``True`` when the two sizes are the same size.
        """
        if self.px is not None and other.px is not None:
            return abs(self.px - other.px) < 0.01
        return self.unit == other.unit and abs(self.value - other.value) < 1e-6


@dataclass(frozen=True, slots=True)
class BookContext:
    """What the rewrite passes need to know about the whole book (TR-NORM-7)."""

    base_size: FontSize | None = None
    root_classes: frozenset[str] = frozenset()
    root_ids: frozenset[str] = frozenset()
    kept_families: frozenset[str] = frozenset()  # lower-case family names
    no_blank_lines: frozenset[str] = frozenset()  # manifest hrefs: title page and nav

    @property
    def base_px(self) -> float:
        """``base_size.px`` when the base is absolute, else ``units.BASE_FONT_PX`` (16.0)."""
        if self.base_size is not None and self.base_size.px:
            return self.base_size.px
        return BASE_FONT_PX


@dataclass(slots=True)
class _Pool:
    """Candidate font sizes that are the same size, with their pooled weight."""

    size: FontSize
    weight: int


def parse_font_size(value: list[Any]) -> FontSize | None:
    """The size of a ``font-size`` value made of one dimension or percentage token.

    Whitespace and comments are ignored.

    Args:
        value: A tinycss2 component-value list of a ``font-size`` declaration.

    Returns:
        The size, or ``None`` for keywords, ``calc()`` and several tokens.
    """
    tokens = [node for node in value if node.type not in ("whitespace", "comment")]
    if len(tokens) != 1:
        return None
    node = tokens[0]
    if node.type == "dimension":
        return FontSize(float(node.value), node.unit.lower())
    if node.type == "percentage":
        return FontSize(float(node.value), "%")
    return None


def survey_book(doc: EpubDocument, options: NormalizeOptions, *, name: str = "") -> BookContext:
    """Read every CSS member and every XHTML member of *doc* and return the book's context.

    Markers are ignored: a merge may append raw chapters to a normalised book. Never writes.

    Args:
        doc: The open EPUB document.
        options: The normalization options; ``strip_fonts`` decides whether families are kept.
        name: The EPUB's file name, which only labels the DEBUG line.

    Returns:
        The book's context.
    """
    manifest = doc.opf.manifest()
    chapters = _texts(doc, (i for i in manifest if _is_xhtml(i)), "xml")
    css_texts = _texts(doc, (i for i in manifest if _is_css(i)), "css")
    svg_members = _texts(doc, (i for i in manifest if _is_svg(i)), "xml")
    census, styled = _census(chapters)
    root_classes, root_ids = _root_names(census)
    book = BookContext(root_classes=root_classes, root_ids=root_ids)
    style_blocks = [m.group(1) for chapter in chapters for m in _STYLE_RE.finditer(chapter)]
    rules = [rule for text in css_texts + style_blocks for rule in _css_rules(text)]
    block_count = sum(count for (tag, _classes, _id), count in census.items() if tag in BLOCK_TAGS)
    base = _base(_pools(rules, styled, census, book), block_count)
    families: set[str] = set()
    if options.strip_fonts:
        plain = bool(getattr(options, "plain_drop_caps", False))
        families = _css_families(rules, book, plain) | _svg_families(
            [m.group(0) for chapter in chapters for m in _SVG_RE.finditer(chapter)] + svg_members
        )
    logger.debug(
        "Normalize survey %s: base font size %s, %d block elements, %d root-only classes",
        name,
        "none" if base is None else f"{base.value:g}{base.unit}",
        block_count,
        len(root_classes),
    )
    return BookContext(
        base_size=base,
        root_classes=root_classes,
        root_ids=root_ids,
        kept_families=frozenset(families),
        no_blank_lines=_no_blank_lines(doc),
    )


def _is_xhtml(item: ManifestItem) -> bool:
    """Return whether a manifest item is an XHTML chapter, as the normalizer detects them.

    Args:
        item: The manifest item to test.

    Returns:
        ``True`` for an XHTML media type or extension, never for SVG.
    """
    if item.media_type.lower() == "image/svg+xml":
        return False
    return item.media_type.lower() == "application/xhtml+xml" or item.href.lower().endswith(
        _XHTML_SUFFIXES
    )


def _is_css(item: ManifestItem) -> bool:
    """Return whether a manifest item is a stylesheet, by media type or extension.

    Args:
        item: The manifest item to test.

    Returns:
        ``True`` for a CSS media type or a ``.css`` file.
    """
    return item.media_type.lower() == "text/css" or item.href.lower().endswith(".css")


def _is_svg(item: ManifestItem) -> bool:
    """Return whether a manifest item is an SVG document.

    Args:
        item: The manifest item to test.

    Returns:
        ``True`` for the SVG media type.
    """
    return item.media_type.lower() == "image/svg+xml"


def _texts(
    doc: EpubDocument, items: Iterable[ManifestItem], kind: Literal["css", "xml"]
) -> list[str]:
    """Return the decoded text of each item that can be read, skipping the others.

    Args:
        doc: The open EPUB document.
        items: The manifest items to read.
        kind: The encoding declaration to decode with.

    Returns:
        The decoded texts, in manifest order.
    """
    texts: list[str] = []
    for item in items:
        text = _member_text(doc, item.href, kind)
        if text is not None:
            texts.append(text)
    return texts


def _member_text(doc: EpubDocument, href: str, kind: Literal["css", "xml"]) -> str | None:
    """Return one member's decoded text, or ``None`` when it is missing or undecodable.

    Args:
        doc: The open EPUB document.
        href: The member's manifest-relative href.
        kind: The encoding declaration to decode with.

    Returns:
        The decoded text, or ``None``.
    """
    try:
        raw = doc.resource_bytes(href)
    except (ItemNotFoundError, KeyError):
        return None
    decoded = decode_declared(raw, kind=kind)
    return decoded[0] if decoded is not None else None


def _attributes(text: str) -> dict[str, str]:
    """Return the attributes of a start tag, names lower-cased and values unquoted.

    Args:
        text: The attribute part of a start tag.

    Returns:
        The attribute values by name.
    """
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(text):
        value = match.group(4)
        attrs[match.group(2).lower()] = value.strip("\"'") if value is not None else ""
    return attrs


def _start_tags(text: str) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield each start tag of *text* as its lower-cased tag name and attributes.

    Args:
        text: The markup to scan.

    Yields:
        ``(tag, attributes)`` for every start tag, in document order.
    """
    for match in START_TAG_RE.finditer(text):
        yield match.group(1).lower(), _attributes(match.group(2))


def _census(chapters: list[str]) -> tuple[Counter[_Entry], list[tuple[str, str]]]:
    """Count the start tags of the chapters, and collect the ``style`` attributes.

    Comments, ``<style>``, ``<script>``, ``<svg>`` and ``<math>`` are masked out first.

    Args:
        chapters: The decoded XHTML chapters.

    Returns:
        The count of each ``(tag, classes, id)`` entry, and ``(tag, style)`` for each element
        with a ``style`` attribute.
    """
    counts: Counter[_Entry] = Counter()
    styled: list[tuple[str, str]] = []
    for chapter in chapters:
        for tag, attrs in _start_tags(_CENSUS_MASK_RE.sub(" ", chapter)):
            classes = frozenset(attrs.get("class", "").split())
            counts[(tag, classes, attrs.get("id") or None)] += 1
            if "style" in attrs:
                styled.append((tag, attrs["style"]))
    return counts, styled


def _root_names(census: Counter[_Entry]) -> tuple[frozenset[str], frozenset[str]]:
    """Return the classes and ids used on an ``html`` or ``body`` tag and on no other tag.

    Args:
        census: The count of each start-tag entry.

    Returns:
        The root-only class names and the root-only ids.
    """
    root_classes: set[str] = set()
    other_classes: set[str] = set()
    root_ids: set[str] = set()
    other_ids: set[str] = set()
    for tag, classes, element_id in census:
        ids = {element_id} if element_id else set()
        if tag in ROOT_TAGS:
            root_classes |= classes
            root_ids |= ids
        else:
            other_classes |= classes
            other_ids |= ids
    return frozenset(root_classes - other_classes), frozenset(root_ids - other_ids)


def _css_rules(text: str) -> list[_Rule]:
    """Return the qualified rules of a stylesheet, including those inside ``@media``/``@supports``.

    Args:
        text: The stylesheet text.

    Returns:
        ``(selectors, declarations)`` for each qualified rule, in source order.
    """
    rules: list[_Rule] = []
    _collect_rules(tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True), rules)
    return rules


def _collect_rules(nodes: list[Any], rules: list[_Rule]) -> None:
    """Append the qualified rules among *nodes* to *rules*, descending into conditional rules.

    Args:
        nodes: Parsed top-level or nested rule nodes.
        rules: The list the rules are appended to.
    """
    for node in nodes:
        if node.type == "qualified-rule":
            rules.append((parse_selector_list(node.prelude), _declarations(node.content)))
        elif (
            node.type == "at-rule"
            and node.lower_at_keyword in ("media", "supports")
            and node.content is not None
        ):
            nested = tinycss2.parse_rule_list(
                node.content, skip_comments=True, skip_whitespace=True
            )
            _collect_rules(nested, rules)


def _declarations(content: Any) -> list[Any]:
    """Return the declarations of a declaration list, skipping errors and comments.

    Args:
        content: A declaration block's tokens, or a ``style`` attribute's text.

    Returns:
        The declaration nodes, in source order.
    """
    parsed = tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True)
    return [node for node in parsed if node.type == "declaration"]


def _font_sizes(declarations: list[Any]) -> list[FontSize]:
    """Return the readable ``font-size`` values among *declarations*.

    Args:
        declarations: Declaration nodes.

    Returns:
        The sizes, in order.
    """
    sizes: list[FontSize] = []
    for declaration in declarations:
        if declaration.lower_name == "font-size":
            size = parse_font_size(declaration.value)
            if size is not None:
                sizes.append(size)
    return sizes


def _subject_weight(subject: Compound, census: Counter[_Entry]) -> int:
    """Return how many census elements a selector's subject compound addresses.

    Args:
        subject: The selector's subject compound.
        census: The count of each start-tag entry.

    Returns:
        The summed count of matching entries; ``0`` for a pseudo-element subject.
    """
    if subject.pseudo_elements:
        return 0
    return sum(
        count
        for (tag, classes, element_id), count in census.items()
        if _matches(subject, tag, classes, element_id)
    )


def _matches(subject: Compound, tag: str, classes: frozenset[str], element_id: str | None) -> bool:
    """Return whether a census entry matches a subject compound.

    Args:
        subject: The selector's subject compound.
        tag: The entry's tag name.
        classes: The entry's class names.
        element_id: The entry's id, or ``None``.

    Returns:
        ``True`` when the tag, every class and every id of the subject match the entry.
    """
    return (
        (subject.tag is None or subject.tag == tag)
        and set(subject.classes) <= classes
        and all(element_id == ident for ident in subject.ids)
    )


def _rule_weight(
    selectors: tuple[Selector, ...], census: Counter[_Entry], book: BookContext
) -> int:
    """Return the weight a rule's font-size gives its size.

    Each selector contributes its subject's census weight unless its context is vector,
    drop cap or root.

    Args:
        selectors: The rule's selectors.
        census: The count of each start-tag entry.
        book: The book, supplying its root-only names.

    Returns:
        The summed weight of the selectors that count.
    """
    weight = 0
    for selector in selectors:
        context = context_of(selector, book)
        if context.vector or context.drop_cap or context.root:
            continue
        weight += _subject_weight(selector.subject, census)
    return weight


def _add_to_pools(pools: list[_Pool], size: FontSize, weight: int) -> None:
    """Pool a candidate size with the first pool of the same size, else open a new pool.

    Args:
        pools: The pools so far; the matching one is updated in place.
        size: The candidate size.
        weight: The candidate's weight.
    """
    for pool in pools:
        if pool.size.same_as(size):
            pool.weight += weight
            return
    pools.append(_Pool(size, weight))


def _pools(
    rules: list[_Rule],
    styled: list[tuple[str, str]],
    census: Counter[_Entry],
    book: BookContext,
) -> list[_Pool]:
    """Pool every readable font-size candidate of the book by size.

    A rule's sizes are weighed by its selectors. A ``style`` attribute weighs 1, or 0 on
    ``html`` and ``body``.

    Args:
        rules: The book's qualified rules.
        styled: ``(tag, style)`` for each element with a ``style`` attribute.
        census: The count of each start-tag entry.
        book: The book, supplying its root-only names.

    Returns:
        The pools, in the order their sizes were first seen.
    """
    pools: list[_Pool] = []
    for selectors, declarations in rules:
        sizes = _font_sizes(declarations)
        if sizes:
            weight = _rule_weight(selectors, census, book)
            for size in sizes:
                _add_to_pools(pools, size, weight)
    for tag, style in styled:
        weight = 0 if tag in ROOT_TAGS else 1
        for size in _font_sizes(_declarations(style)):
            _add_to_pools(pools, size, weight)
    return pools


def _base(pools: list[_Pool], block_count: int) -> FontSize | None:
    """Return the heaviest size when it weighs at least half the block count.

    Args:
        pools: The pooled sizes.
        block_count: The number of block elements in the book.

    Returns:
        The body-text size, or ``None`` when no size is trusted.
    """
    if block_count <= 0 or not pools:
        return None
    heaviest = max(pools, key=lambda pool: pool.weight)
    return heaviest.size if heaviest.weight >= BASE_SHARE * block_count else None


def _css_families(rules: list[_Rule], book: BookContext, plain_drop_caps: bool) -> set[str]:
    """Return the families that a vector or drop-cap rule sets.

    Args:
        rules: The book's qualified rules.
        book: The book, supplying its root-only names.
        plain_drop_caps: Whether drop caps are plain, so their families are not kept.

    Returns:
        The lower-case family names.
    """
    names: set[str] = set()
    for selectors, declarations in rules:
        contexts = [context_of(selector, book) for selector in selectors]
        exempt = any(c.vector or (c.drop_cap and not plain_drop_caps) for c in contexts)
        if not exempt:
            continue
        for declaration in declarations:
            names.update(_declared_families(declaration))
    return names


def _declared_families(declaration: Any) -> list[str]:
    """Return the family names a ``font-family`` or readable ``font`` declaration sets.

    Args:
        declaration: A declaration node.

    Returns:
        The lower-case family names, or an empty list.
    """
    if declaration.lower_name == "font-family":
        return family_names(declaration.value)
    if declaration.lower_name == "font":
        return _shorthand_family(declaration.value)
    return []


def _shorthand_family(value: list[Any]) -> list[str]:
    """Return the family names of a readable ``font`` shorthand.

    Args:
        value: The tokens of a ``font`` declaration.

    Returns:
        The lower-case family names, or an empty list when the shorthand is not readable.
    """
    if expand_font_shorthand(value) is None:
        return []
    tokens = [node for node in value if node.type not in ("whitespace", "comment")]
    size_index = next(
        (
            index
            for index, node in enumerate(tokens)
            if node.type in ("dimension", "percentage")
            or (node.type == "ident" and node.lower_value in FONT_SIZE_KEYWORDS)
        ),
        None,
    )
    if size_index is None:
        return []
    rest = tokens[size_index + 1 :]
    if rest and rest[0].type == "literal" and rest[0].value == "/":
        rest = rest[2:]
    return family_names(rest)


def _svg_families(texts: list[str]) -> set[str]:
    """Return the font families that SVG text and style attributes set.

    Args:
        texts: SVG markup: the subtrees of XHTML chapters and whole SVG documents.

    Returns:
        The lower-case family names.
    """
    names: set[str] = set()
    for text in texts:
        for _tag, attrs in _start_tags(text):
            if "font-family" in attrs:
                names.update(
                    family_names(tinycss2.parse_component_value_list(attrs["font-family"]))
                )
            if "style" in attrs:
                for declaration in _declarations(attrs["style"]):
                    if declaration.lower_name == "font-family":
                        names.update(family_names(declaration.value))
    return names


def _no_blank_lines(doc: EpubDocument) -> frozenset[str]:
    """Return the hrefs of the title page and of every navigation document.

    Args:
        doc: The open EPUB document.

    Returns:
        The manifest hrefs that get no blank-line marking.
    """
    title_id = doc.title_page_item_id()
    return frozenset(
        item.href
        for item in doc.opf.manifest()
        if item.id == title_id or "nav" in item.properties.split()
    )
