"""The selector model: each selector of a rule decides its own context (TR-NORM-6).

A rule's selector list is parsed into selectors, and each selector into compounds, from its
tinycss2 prelude. Its context (vector, drop cap, root, heading, image) is then decided from
those compounds alone, so one selector's decision never leaks into its neighbours. This
replaces whole-list regexes, which over-matched: ``p.chapter-image-caption`` was exempt as an
image, and ``h1, p.dropcap`` was exempt as a whole because one part matched.

Matching is lexical only. Selectors are never matched against the DOM, so a decision is the
same for every document that uses the stylesheet. The root-only class and id names come from
the book, which is the only place the book-wide view enters.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import tinycss2

VECTOR_TAGS: frozenset[str] = frozenset({"svg", "math", "image"})
VECTOR_NAMESPACES: frozenset[str] = frozenset({"svg", "math", "m", "mml", "mathml"})
HEADING_TAGS: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
ROOT_TAGS: frozenset[str] = frozenset({"html", "body"})
IMAGE_TAGS: frozenset[str] = frozenset(
    {"img", "svg", "image", "figure", "picture", "video", "object", "embed", "canvas"}
)
DROP_CAP_PSEUDO_ELEMENTS: frozenset[str] = frozenset({"first-letter", "first-line"})
LEGACY_PSEUDO_ELEMENTS: frozenset[str] = frozenset(
    {"first-letter", "first-line", "before", "after"}
)
DROP_CAP_NAME_RE: re.Pattern[str] = re.compile(r"(?:drop|initial)[-_]?cap|first[-_]?letter", re.I)
IMAGE_NAME_RE: re.Pattern[str] = re.compile(
    r"cover|title[-_]?page|image|img|figure|illus|photo|picture", re.I
)

_NAME_TOKEN_TYPES = ("ident", "function")


class _RootNames(Protocol):
    """What the selector model reads of the book: its root-only class and id names."""

    @property
    def root_classes(self) -> frozenset[str]:
        """The classes used only on the root."""

    @property
    def root_ids(self) -> frozenset[str]:
        """The ids used only on the root."""


@dataclass(frozen=True, slots=True)
class Compound:
    """One compound selector: a type, classes, ids and pseudo-classes/elements, no combinator."""

    tag: str | None = None
    namespace: str | None = None
    classes: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()
    pseudo_classes: tuple[str, ...] = ()
    pseudo_elements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Selector:
    """One complex selector of a rule's selector list."""

    text: str
    compounds: tuple[Compound, ...]

    @property
    def subject(self) -> Compound:
        """The rightmost compound (an empty ``Compound()`` when there is none).

        Returns:
            The compound the selector applies to.
        """
        return self.compounds[-1] if self.compounds else Compound()


@dataclass(frozen=True, slots=True)
class RuleContext:
    """How the rules treat one selector (FR-NORM-10, TR-NORM-6).

    The flags are independent: a selector may be both ``vector`` and ``image``.
    """

    vector: bool = False
    drop_cap: bool = False
    root: bool = False
    heading: bool = False
    image: bool = False


@dataclass(slots=True)
class _Parts:
    """The fields of one compound, collected while its tokens are read."""

    tag: str | None = None
    namespace: str | None = None
    classes: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    pseudo_classes: list[str] = field(default_factory=list)
    pseudo_elements: list[str] = field(default_factory=list)


def _at(tokens: list[Any], index: int) -> Any:
    """Return the token at *index*, or ``None`` past the end.

    Args:
        tokens: The tokens to index.
        index: The position to read.

    Returns:
        The token, or ``None`` when *index* is past the end.
    """
    return tokens[index] if index < len(tokens) else None


def _is_literal(token: Any, *values: str) -> bool:
    """Return whether *token* is a literal token whose value is one of *values*.

    Args:
        token: The token to test, or ``None``.
        *values: The literal values that match.

    Returns:
        ``True`` when the token is a literal with a matching value.
    """
    return token is not None and token.type == "literal" and token.value in values


def _is_name(token: Any) -> bool:
    """Return whether *token* can name a pseudo-class or pseudo-element.

    Args:
        token: The token to test, or ``None``.

    Returns:
        ``True`` for an identifier or a function token.
    """
    return token is not None and token.type in _NAME_TOKEN_TYPES


def _name(token: Any) -> str:
    """Return the lower-cased name of an identifier or function token.

    Args:
        token: An identifier or function token.

    Returns:
        The lower-cased name.
    """
    text: str = token.value if token.type == "ident" else token.name
    return text.lower()


def _take_pseudo(tokens: list[Any], index: int, parts: _Parts) -> int:
    """Read a pseudo-class or pseudo-element starting at the colon at *index*.

    Args:
        tokens: The compound's tokens.
        index: The position of the first colon.
        parts: The compound being built; the pseudo is appended to it.

    Returns:
        The index of the first token after the pseudo.
    """
    second = _at(tokens, index + 1)
    if _is_literal(second, ":"):
        name = _at(tokens, index + 2)
        if _is_name(name):
            parts.pseudo_elements.append(_name(name))
            return index + 3
        return index + 2
    if _is_name(second):
        name_text = _name(second)
        bucket = (
            parts.pseudo_elements if name_text in LEGACY_PSEUDO_ELEMENTS else parts.pseudo_classes
        )
        bucket.append(name_text)
        return index + 2
    return index + 1


def _take(tokens: list[Any], index: int, parts: _Parts) -> int:
    """Read the one simple-selector token group starting at *index* into *parts*.

    Args:
        tokens: The compound's tokens.
        index: The position of the token to read.
        parts: The compound being built.

    Returns:
        The index of the first token after the group.
    """
    token = tokens[index]
    following = _at(tokens, index + 1)
    if _is_literal(following, "|") and (token.type == "ident" or _is_literal(token, "*")):
        parts.namespace = token.value.lower() if token.type == "ident" else None
        return index + 2
    if token.type == "ident":
        parts.tag = token.value.lower()
    elif token.type == "hash":
        parts.ids.append(token.value)
    elif _is_literal(token, ".") and following is not None and following.type == "ident":
        parts.classes.append(following.value)
        return index + 2
    elif _is_literal(token, ":"):
        return _take_pseudo(tokens, index, parts)
    return index + 1


def _compound(tokens: list[Any]) -> Compound:
    """Build the compound for one run of tokens that has no combinator or whitespace.

    Args:
        tokens: The tokens of the compound, in source order.

    Returns:
        The compound. Tokens that are not part of the model (attribute blocks, ``*``)
        contribute nothing.
    """
    parts = _Parts()
    index = 0
    while index < len(tokens):
        index = _take(tokens, index, parts)
    return Compound(
        tag=parts.tag,
        namespace=parts.namespace,
        classes=tuple(parts.classes),
        ids=tuple(parts.ids),
        pseudo_classes=tuple(parts.pseudo_classes),
        pseudo_elements=tuple(parts.pseudo_elements),
    )


def _selector(part: list[Any]) -> Selector | None:
    """Build the selector for one comma-separated part of a selector list.

    Args:
        part: The tokens of one selector, between commas.

    Returns:
        The selector, or ``None`` when the part is blank.
    """
    text = tinycss2.serialize(part).strip()
    if not text:
        return None
    compounds: list[Compound] = []
    run: list[Any] = []
    for token in part:
        if token.type in ("whitespace", "comment") or _is_literal(token, ">", "+", "~"):
            if run:
                compounds.append(_compound(run))
                run = []
        else:
            run.append(token)
    if run:
        compounds.append(_compound(run))
    return Selector(text=text, compounds=tuple(compounds))


def parse_selector_list(prelude: list[Any]) -> tuple[Selector, ...]:
    """Split a qualified rule's prelude (tinycss2 tokens) into its selectors.

    Args:
        prelude: The tokens before the rule's ``{`` block.

    Returns:
        The non-blank selectors, in source order.
    """
    parts: list[list[Any]] = [[]]
    for token in prelude:
        if _is_literal(token, ","):
            parts.append([])
        else:
            parts[-1].append(token)
    selectors = (_selector(part) for part in parts)
    return tuple(selector for selector in selectors if selector is not None)


def element_selector(tag: str, class_attr: str | None, id_attr: str | None) -> Selector:
    """The one-compound selector of an element start tag.

    Args:
        tag: The element's tag name; it is lower-cased.
        class_attr: The ``class`` attribute, split on whitespace, or ``None``.
        id_attr: The ``id`` attribute, or ``None``.

    Returns:
        A selector whose text is the tag, then ``.class`` per class, then ``#id``.
    """
    classes = tuple((class_attr or "").split())
    ids = (id_attr,) if id_attr else ()
    text = (
        tag.lower() + "".join(f".{name}" for name in classes) + "".join(f"#{name}" for name in ids)
    )
    return Selector(text=text, compounds=(Compound(tag=tag.lower(), classes=classes, ids=ids),))


def _names(compound: Compound) -> tuple[str, ...]:
    """Return a compound's class and id names.

    Args:
        compound: The compound to read.

    Returns:
        Its classes followed by its ids.
    """
    return compound.classes + compound.ids


def _vector(compounds: tuple[Compound, ...]) -> bool:
    """Return whether any compound is an SVG or MathML element or namespace.

    Args:
        compounds: The selector's compounds.

    Returns:
        ``True`` for a vector tag or vector namespace in any compound.
    """
    return any(c.tag in VECTOR_TAGS or c.namespace in VECTOR_NAMESPACES for c in compounds)


def _image(compounds: tuple[Compound, ...]) -> bool:
    """Return whether any compound is an image element or carries an image-like name.

    Over-matching a class that only contains an image word keeps dimensions, which is the
    safe direction.

    Args:
        compounds: The selector's compounds.

    Returns:
        ``True`` for an image tag or an image-named class or id.
    """
    if any(c.tag in IMAGE_TAGS for c in compounds):
        return True
    return any(IMAGE_NAME_RE.search(name) for c in compounds for name in _names(c))


def _drop_cap(subject: Compound) -> bool:
    """Return whether the subject is a drop-cap selector.

    Args:
        subject: The selector's subject compound.

    Returns:
        ``True`` for a drop-cap pseudo-element or a drop-cap-named class or id.
    """
    if any(p in DROP_CAP_PSEUDO_ELEMENTS for p in subject.pseudo_elements):
        return True
    return any(DROP_CAP_NAME_RE.search(name) for name in _names(subject))


def _only_on_root(subject: Compound, book: _RootNames) -> bool:
    """Return whether the subject names only root-only classes and ids.

    Args:
        subject: The selector's subject compound.
        book: The book, supplying its root-only class and id names.

    Returns:
        ``True`` when the subject is an untagged or root-tagged compound with at least one
        name, and every name is used only on the root.
    """
    if subject.tag is not None and subject.tag not in ROOT_TAGS:
        return False
    if not _names(subject):
        return False
    return set(subject.classes) <= book.root_classes and set(subject.ids) <= book.root_ids


def _root(subject: Compound, book: _RootNames) -> bool:
    """Return whether the subject addresses the document root.

    Args:
        subject: The selector's subject compound.
        book: The book, supplying its root-only class and id names.

    Returns:
        ``True`` for ``html``, ``body``, ``:root``, or a root-only name; never for a
        pseudo-element.
    """
    if subject.pseudo_elements:
        return False
    if subject.tag in ROOT_TAGS or "root" in subject.pseudo_classes:
        return True
    return _only_on_root(subject, book)


def context_of(selector: Selector, book: _RootNames) -> RuleContext:
    """Decide *selector*'s ``RuleContext`` against the book's root-only classes and ids.

    Args:
        selector: The selector to classify.
        book: The book, supplying its root-only class and id names.

    Returns:
        The selector's independent context flags.
    """
    subject = selector.subject
    return RuleContext(
        vector=_vector(selector.compounds),
        drop_cap=_drop_cap(subject),
        root=_root(subject, book),
        heading=subject.tag in HEADING_TAGS,
        image=_image(selector.compounds),
    )


def group_by_context(
    selectors: Sequence[Selector], book: _RootNames
) -> list[tuple[RuleContext, list[Selector]]]:
    """The selectors grouped by context, groups in order of first appearance (TR-NORM-6).

    Args:
        selectors: The selectors to group.
        book: The book, supplying its root-only class and id names.

    Returns:
        ``(context, selectors)`` pairs, one per distinct context, ordered by the first
        selector that has each context.
    """
    groups: dict[RuleContext, list[Selector]] = {}
    for selector in selectors:
        groups.setdefault(context_of(selector, book), []).append(selector)
    return list(groups.items())
