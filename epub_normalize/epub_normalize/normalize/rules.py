"""EPUB normalization rule constants and selector predicates (FR-NORM-10, TR-NORM-4).

Declares which CSS properties each normalization switch governs, and provides
selector-position predicates that identify drop-cap, image, and root contexts
where different rules apply. This module is the single source of truth for these
constants, so the declaration rewriter, stylesheet rewriter, and tests all read
the same rules.

**Property sets by function:**

- **FONT_PROPERTIES** — `font-family` and the `font` shorthand. The shorthand
  is removed whole because it carries the family and there is no safe partial
  edit; the direct blocker to a reader's font choice. Controlled by
  `strip_fonts=True`.

- **COLOR_PROPERTIES** — `color` and `background-color`. A hardcoded near-black
  on near-white is unreadable in a dark theme, and a hardcoded background
  defeats sepia/night modes. Controlled by `strip_colors=True`.

- **COLOR_SHORTHAND_PROPERTIES** — the `background` shorthand. Removed **only**
  when its value contains no `url(`, because a shorthand carrying an image is a
  real asset reference, not a colour. Controlled by `strip_colors=True`.

- **SPACING_PROPERTIES** — `line-height`, `letter-spacing`, and `word-spacing`.
  These override the reader's own spacing controls and break visibly once the
  user raises the font size. Controlled by `strip_line_spacing=True`.

- **DIMENSION_PROPERTIES** — `width`, `height`, `min-width`, `min-height`, and
  `max-height` (absolute lengths only). A fixed text-container width produces
  horizontal scrolling on a phone. Controlled by `strip_fixed_dimensions=True`.
  `max-width` is never removed; it is the responsive constraint, not the problem.

- **ALIGNMENT_PROPERTIES** — `text-align` and `text-indent`. Preserved by
  default: centring is meaningful in poetry, signatures, and chapter headings,
  and an indent is typographic intent, not interference. Controlled by
  `preserve_alignment=False` (inverted: preserved by default).

- **BOX_PROPERTIES** — `margin`, `margin-*`, `padding`, `padding-*`, and
  `text-indent` (absolute lengths only). Converted to `em` only when margin
  normalization is on (off by default). `text-indent` appears here and in
  ALIGNMENT_PROPERTIES; alignment takes precedence in the decision tree.
  Controlled by `normalize_margins=False`.

- **NEVER_STRIPPED_PROPERTIES** — `max-width`. Never removed at any selector
  or any setting, because it is the responsive constraint.

- **FONT_SIZE_PROPERTY** — `font-size`. Absolute lengths convert to `em` on a
  16 px base; when the selector is `html` or `body`, the property is removed
  outright rather than converted, because that value *is* the reader's base
  size and any value we leave there wins over the user's control. Controlled by
  `convert_font_sizes=True`.

**Selector predicates:**

- **is_preserved_selector** — a rule whose selector matches `::first-letter`,
  `:first-letter`, `::first-line`, `dropcap`, `drop-cap`, `initial-cap`,
  `first-letter`, `svg`, `image`, or `math` is exempt from **every** strip and
  transform, whole. Drop caps legitimately need a large absolute size, a
  specific family, and a colour.

- **is_image_selector** — dimension stripping skips any rule whose selector
  mentions `img`, `svg`, `image`, `figure`, `.cover`, or `.titlepage`, so a
  cover's aspect ratio and an image's sizing survive. Used only for dimension
  stripping, not for other properties.

- **is_root_selector** — matches only a *selector-position* `html` or `body`,
  never a class or id spelled the same way. The pattern anchors on start or a
  combinator and looks ahead for end or a delimiter, so `.body`, `bodytext`,
  and `#body` never match. This is what makes `font-size` on `html`/`body` get
  removed rather than converted.
"""

from __future__ import annotations

import re

FONT_PROPERTIES: frozenset[str] = frozenset({"font-family", "font"})

COLOR_PROPERTIES: frozenset[str] = frozenset({"color", "background-color"})

COLOR_SHORTHAND_PROPERTIES: frozenset[str] = frozenset({"background"})

SPACING_PROPERTIES: frozenset[str] = frozenset({"line-height", "letter-spacing", "word-spacing"})

DIMENSION_PROPERTIES: frozenset[str] = frozenset(
    {"width", "height", "min-width", "min-height", "max-height"}
)

ALIGNMENT_PROPERTIES: frozenset[str] = frozenset({"text-align", "text-indent"})

BOX_PROPERTIES: frozenset[str] = frozenset(
    {
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "text-indent",
    }
)

FONT_SIZE_PROPERTY = "font-size"

NEVER_STRIPPED_PROPERTIES: frozenset[str] = frozenset({"max-width"})

PRESERVE_SELECTOR_RE: re.Pattern[str] = re.compile(
    r"::?first-letter|::?first-line|drop-?cap|initial-?cap|first-?letter|\bsvg\b|\bimage\b|\bmath\b",
    re.IGNORECASE,
)

IMAGE_SELECTOR_RE: re.Pattern[str] = re.compile(
    r"\bimg\b|\bsvg\b|\bimage\b|\bfigure\b|\.cover|\.titlepage|\.title-page",
    re.IGNORECASE,
)

ROOT_SELECTOR_RE: re.Pattern[str] = re.compile(
    r"(?:^|[\s,>+~])(?:html|body)(?=$|[\s,>+~:.\[])",
    re.IGNORECASE,
)

FONT_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/font-sfnt",
        "application/font-woff",
        "application/vnd.ms-opentype",
        "application/x-font-opentype",
        "application/x-font-truetype",
        "application/x-font-ttf",
        "font/otf",
        "font/ttf",
        "font/woff",
        "font/woff2",
    }
)

FONT_EXTENSIONS: frozenset[str] = frozenset({".eot", ".otf", ".ttc", ".ttf", ".woff", ".woff2"})


def is_preserved_selector(selector: str) -> bool:
    """Check if selector denotes a context where all styles are preserved.

    A selector matching drop-cap, first-letter, first-line, SVG, image, or
    MathML contexts is exempt from every strip and transform.

    Args:
        selector: A CSS selector string.

    Returns:
        True if the selector matches a preserved context, False otherwise.
    """
    return bool(PRESERVE_SELECTOR_RE.search(selector))


def is_image_selector(selector: str) -> bool:
    """Check if selector denotes an image context where dimensions are preserved.

    Used only for dimension stripping; dimension rules skip any selector
    mentioning images, figures, covers, or title pages, so their aspect ratio
    and sizing survive.

    Args:
        selector: A CSS selector string.

    Returns:
        True if the selector matches an image context, False otherwise.
    """
    return bool(IMAGE_SELECTOR_RE.search(selector))


def is_root_selector(selector: str) -> bool:
    """Check if selector matches html or body at the selector position.

    Matches only a *selector-position* `html` or `body`, never a class or id
    spelled the same way. This is what makes `font-size` on `html`/`body` get
    removed rather than converted, because that value *is* the reader's base
    size.

    Args:
        selector: A CSS selector string.

    Returns:
        True if the selector is html or body at the selector position, False otherwise.
    """
    return bool(ROOT_SELECTOR_RE.search(selector))
