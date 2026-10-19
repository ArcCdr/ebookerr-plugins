"""Tests for the EPUB normalizer's selector model: parsing, per-selector context, grouping."""

from __future__ import annotations

from types import SimpleNamespace

import tinycss2
from epub_normalize.normalize.selectors import (
    Compound,
    RuleContext,
    context_of,
    element_selector,
    group_by_context,
    parse_selector_list,
)

book = SimpleNamespace(root_classes=frozenset({"c9"}), root_ids=frozenset())


def _sel(text: str):
    """Parse a selector list written as CSS text into selectors."""
    return parse_selector_list(tinycss2.parse_stylesheet(text + " {}")[0].prelude)


class TestParsing:
    """The selector list is split into selectors and each selector into compounds."""

    def test_pseudo_element_after_class(self) -> None:
        """A pseudo-element after a class is recorded on the compound."""
        selectors = _sel("p.first::first-letter")
        assert len(selectors) == 1
        assert selectors[0].compounds == (
            Compound(tag="p", classes=("first",), pseudo_elements=("first-letter",)),
        )

    def test_single_colon_legacy_pseudo_element(self) -> None:
        """A single-colon legacy pseudo-element is a pseudo-element, not a pseudo-class."""
        assert _sel("p:first-letter")[0].subject.pseudo_elements == ("first-letter",)

    def test_selector_list_split_and_texts(self) -> None:
        """A comma splits the list; each selector keeps its stripped source text."""
        selectors = _sel("h1, .a.b > span#x")
        assert [s.text for s in selectors] == ["h1", ".a.b > span#x"]
        assert selectors[1].compounds == (
            Compound(classes=("a", "b")),
            Compound(tag="span", ids=("x",)),
        )

    def test_pseudo_class_with_function(self) -> None:
        """A functional pseudo-class is recorded by name."""
        assert _sel("body:not(.x) p")[0].compounds == (
            Compound(tag="body", pseudo_classes=("not",)),
            Compound(tag="p"),
        )

    def test_universal_type_sets_no_tag(self) -> None:
        """A universal type leaves the tag unset."""
        assert _sel("*.cover img")[0].compounds == (
            Compound(classes=("cover",)),
            Compound(tag="img"),
        )

    def test_attribute_selector_is_ignored(self) -> None:
        """An attribute selector contributes nothing to the compound."""
        assert _sel("a[href]")[0].subject == Compound(tag="a")

    def test_root_pseudo_class(self) -> None:
        """``:root`` is a pseudo-class on a compound with no tag."""
        assert _sel(":root")[0].subject == Compound(pseudo_classes=("root",))

    def test_namespace_prefix(self) -> None:
        """A namespace prefix is recorded separately from the type."""
        assert _sel("svg|rect")[0].subject == Compound(tag="rect", namespace="svg")


class TestContext:
    """Each selector's context is decided from its compounds and the book's root names."""

    def test_image_over_match_on_class_name(self) -> None:
        """An image word inside a class name marks the selector as image."""
        assert context_of(_sel("p.chapter-image-caption")[0], book) == RuleContext(image=True)

    def test_drop_cap_by_class_name(self) -> None:
        """A drop-cap class name marks the selector as drop cap."""
        assert context_of(_sel("p.dropcap")[0], book) == RuleContext(drop_cap=True)

    def test_drop_cap_by_initial_cap_class(self) -> None:
        """An initial-cap class name marks the selector as drop cap."""
        assert context_of(_sel("span.initial-cap")[0], book) == RuleContext(drop_cap=True)

    def test_drop_cap_by_pseudo_element(self) -> None:
        """The first-line pseudo-element marks the selector as drop cap."""
        assert context_of(_sel("p::first-line")[0], book) == RuleContext(drop_cap=True)

    def test_svg_descendant_is_vector_and_image(self) -> None:
        """An svg compound makes the selector both vector and image."""
        assert context_of(_sel("svg text")[0], book) == RuleContext(vector=True, image=True)

    def test_mathml_is_vector(self) -> None:
        """A math compound makes the selector vector only."""
        assert context_of(_sel("math mi")[0], book) == RuleContext(vector=True)

    def test_namespaced_vector(self) -> None:
        """A vector namespace prefix makes the selector vector."""
        assert context_of(_sel("svg|rect")[0], book).vector is True

    def test_body_descendant_is_plain(self) -> None:
        """A descendant of the body is an ordinary selector with no flags."""
        assert context_of(_sel("body p")[0], book) == RuleContext()

    def test_root_selectors(self) -> None:
        """Bare root tags, ``:root``, and root-only classes are root selectors."""
        for text in ("body", "html", ":root", ".c9", "body.c9"):
            assert context_of(_sel(text)[0], book) == RuleContext(root=True), text

    def test_class_used_below_root_is_not_root(self) -> None:
        """A root-only class on a descendant does not make the selector root."""
        assert context_of(_sel(".c9 p")[0], book).root is False

    def test_class_not_in_root_set_is_not_root(self) -> None:
        """A class that is not a root-only class does not make the selector root."""
        assert context_of(_sel(".body")[0], book).root is False

    def test_heading_with_class(self) -> None:
        """A heading tag marks the selector as heading."""
        assert context_of(_sel("h2.title")[0], book) == RuleContext(heading=True)

    def test_image_by_container_class(self) -> None:
        """An image-named class on a container marks the subject's descendant as image."""
        assert context_of(_sel("div.cover img")[0], book) == RuleContext(image=True)

    def test_figure_is_image(self) -> None:
        """A figure tag marks the selector as image."""
        assert context_of(_sel("figure")[0], book) == RuleContext(image=True)


class TestGrouping:
    """Selectors are grouped by context, in order of first appearance."""

    def test_groups_in_first_appearance_order(self) -> None:
        """Same-context selectors share a group; groups follow first appearance."""
        groups = group_by_context(_sel("h1, p.dropcap, h2"), book)
        assert [(ctx, [s.text for s in sels]) for ctx, sels in groups] == [
            (RuleContext(heading=True), ["h1", "h2"]),
            (RuleContext(drop_cap=True), ["p.dropcap"]),
        ]


class TestElementSelector:
    """An element's start tag becomes a one-compound selector."""

    def test_tag_classes_and_id(self) -> None:
        """The tag is lower-cased, classes are split, the id is kept."""
        selector = element_selector("IMG", "cover big", "c1")
        assert selector.text == "img.cover.big#c1"
        assert selector.compounds == (Compound(tag="img", classes=("cover", "big"), ids=("c1",)),)

    def test_body_element_is_root(self) -> None:
        """A body element is a root selector."""
        assert context_of(element_selector("body", None, None), book).root is True

    def test_blank_class_attribute_has_no_classes(self) -> None:
        """A whitespace-only class attribute yields no classes."""
        assert element_selector("p", "  ", None).subject.classes == ()
