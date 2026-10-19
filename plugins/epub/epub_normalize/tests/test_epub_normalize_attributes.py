"""Tests for the HTML presentational attributes each styling level removes.

The pure function under test takes one start tag's attributes and returns the ones that
survive the level's rule groups, order kept. Attribute values arrive raw, quotes included.
"""

from __future__ import annotations

from epub_normalize.normalize.attributes import strip_presentational_attributes
from epub_normalize.normalize.options import options_for_level


def _strip(
    tag: str, attrs: list[tuple[str, str | None]], level: str
) -> list[tuple[str, str | None]]:
    """Strip *attrs* of *tag* under the options of *level*."""
    return strip_presentational_attributes(tag, attrs, options=options_for_level(level))


def test_font_attributes_by_level() -> None:
    attrs = [("color", '"#000000"'), ("face", '"Arial"'), ("size", '"2"')]
    assert _strip("font", attrs, "light") == [("size", '"2"')]
    assert _strip("font", attrs, "standard") == [("size", '"2"')]
    assert _strip("font", attrs, "strict") == []


def test_table_cell_attributes() -> None:
    attrs = [("bgcolor", '"#fff"'), ("align", '"justify"'), ("width", '"200"')]
    assert _strip("td", attrs, "standard") == []
    assert _strip("td", [("align", '"center"'), ("width", '"50%"')], "standard") == [
        ("align", '"center"'),
        ("width", '"50%"'),
    ]
    assert _strip("td", attrs, "light") == [("align", '"justify"'), ("width", '"200"')]


def test_image_like_elements_keep_everything() -> None:
    attrs = [("align", '"left"'), ("width", '"20"')]
    for tag in ("img", "object", "svg", "video", "canvas", "iframe", "embed", "input"):
        assert _strip(tag, attrs, "strict") == attrs, tag


def test_image_like_elements_keep_a_bgcolor() -> None:
    attrs = [("bgcolor", '"#fff"'), ("alt", '"a"')]
    for tag in ("img", "object", "svg", "video", "canvas", "iframe", "embed", "input"):
        assert _strip(tag, attrs, "light") == attrs, tag


def test_body_attributes() -> None:
    attrs = [
        ("text", '"#000"'),
        ("bgcolor", '"#fff"'),
        ("leftmargin", '"20"'),
        ("class", '"c9"'),
    ]
    assert _strip("body", attrs, "light") == [("leftmargin", '"20"'), ("class", '"c9"')]
    assert _strip("body", attrs, "standard") == [("class", '"c9"')]


def test_values_are_read_without_quotes_and_case() -> None:
    assert _strip("p", [("align", "'JUSTIFY'")], "standard") == []
    assert _strip("p", [("align", None)], "standard") == [("align", None)]


def test_order_and_unrelated_attributes_are_kept() -> None:
    attrs = [("id", '"t"'), ("width", '"600px"'), ("class", '"x"')]
    assert _strip("table", attrs, "standard") == [("id", '"t"'), ("class", '"x"')]
