"""EPUB normalization rule constants (FR-NORM-3..10, FR-NORM-16, FR-NORM-17, TR-NORM-4).

The property sets each rule group of the declaration decision tree reads. This module is the
single source of truth for them, so the declaration rewriter, the stylesheet rewriter and the
tests all read the same rules. Which selectors are drop caps, roots, images or vector content
is decided by ``selectors.py``, not here.

**Property sets by function:**

- **COLOR_PROPERTIES** / **COLOR_SHORTHAND_PROPERTIES** / **GRADIENT_FUNCTIONS** — colour
  longhands and shadows (removed whole) and the shorthands that may carry a colour or a
  gradient (rewritten without it). ``BACKGROUND_IMAGE_PROPERTY`` is judged by its value.
- **FONT_FAMILY_PROPERTY** / **FONT_SHORTHAND_PROPERTY** / **MONOSPACE_FAMILIES** — a family
  list is removed unless it names a monospace family; the ``font`` shorthand is expanded.
- **FONT_SIZE_PROPERTY** — ``font-size``.
- **SPACING_PROPERTIES** / **HYPHENATION_PROPERTIES** — removed whole.
- **ALIGNMENT_PROPERTIES** / **BODY_ALIGNMENTS** — removed only when set to a body-text value.
- **FIXED_SIZE_PROPERTIES** — removed when absolute, outside image contexts.
- **ROOT_LAYOUT_PROPERTIES** / **PAGE_MARGIN_DESCRIPTORS** — the page layout, removed on the root.
- **BOX_PROPERTIES** — absolute lengths convert to ``em``.
- **SEMANTIC_PROPERTIES** — what a drop cap keeps at the strict level.
- **FONT_MEDIA_TYPES** / **FONT_EXTENSIONS** — how a font member is recognised.

Never touched at any level: ``white-space``, ``display``, ``float``, ``clear``, ``page-break-*``,
``break-*``, ``widows``, ``orphans``, ``list-style*``, ``content``, ``counter-*``,
``font-weight``, ``font-style``, ``font-variant*``, ``text-transform``, ``vertical-align``,
``direction``, ``unicode-bidi``, ``writing-mode``, border widths and styles, ``max-width``
outside the root, and relative lengths outside the rules above.
"""

from __future__ import annotations

COLOR_PROPERTIES: frozenset[str] = frozenset(
    {
        "color",
        "background-color",
        "border-color",
        "border-top-color",
        "border-right-color",
        "border-bottom-color",
        "border-left-color",
        "border-block-color",
        "border-block-start-color",
        "border-block-end-color",
        "border-inline-color",
        "border-inline-start-color",
        "border-inline-end-color",
        "outline-color",
        "text-decoration-color",
        "text-emphasis-color",
        "column-rule-color",
        "caret-color",
        "accent-color",
        "scrollbar-color",
        "-webkit-text-fill-color",
        "-webkit-text-stroke-color",
        "-webkit-text-stroke",
        "-webkit-tap-highlight-color",
        "text-shadow",
        "box-shadow",
        "-webkit-box-shadow",
        "-moz-box-shadow",
    }
)
"""Colour-bearing longhands (and shadows), removed whole (FR-NORM-4)."""

COLOR_SHORTHAND_PROPERTIES: frozenset[str] = frozenset(
    {
        "background",
        "border",
        "border-top",
        "border-right",
        "border-bottom",
        "border-left",
        "border-block",
        "border-block-start",
        "border-block-end",
        "border-inline",
        "border-inline-start",
        "border-inline-end",
        "outline",
        "text-decoration",
        "text-emphasis",
        "column-rule",
        "-webkit-column-rule",
    }
)
"""Shorthands that may carry a colour: the colour component (and any gradient) is removed and the
rest kept; one left empty is removed (FR-NORM-4)."""

GRADIENT_FUNCTIONS: frozenset[str] = frozenset(
    {
        "linear-gradient",
        "radial-gradient",
        "conic-gradient",
        "repeating-linear-gradient",
        "repeating-radial-gradient",
        "repeating-conic-gradient",
        "-webkit-linear-gradient",
        "-webkit-radial-gradient",
        "-moz-linear-gradient",
        "-webkit-gradient",
    }
)

BACKGROUND_IMAGE_PROPERTY = "background-image"

FONT_FAMILY_PROPERTY = "font-family"
FONT_SHORTHAND_PROPERTY = "font"
FONT_SIZE_PROPERTY = "font-size"

MONOSPACE_FAMILIES: frozenset[str] = frozenset(
    {
        "monospace",
        "ui-monospace",
        "courier",
        "courier new",
        "courier prime",
        "courier std",
        "consolas",
        "menlo",
        "monaco",
        "lucida console",
        "lucida sans typewriter",
        "andale mono",
        "dejavu sans mono",
        "liberation mono",
        "source code pro",
        "fira code",
        "fira mono",
        "inconsolata",
        "sf mono",
        "roboto mono",
        "ubuntu mono",
        "noto sans mono",
        "pt mono",
        "cousine",
    }
)
"""Family names (lower-case) that make a family list monospace (FR-NORM-3)."""

SPACING_PROPERTIES: frozenset[str] = frozenset({"line-height", "letter-spacing", "word-spacing"})

HYPHENATION_PROPERTIES: frozenset[str] = frozenset(
    {
        "hyphens",
        "-webkit-hyphens",
        "-moz-hyphens",
        "-ms-hyphens",
        "-epub-hyphens",
        "adobe-hyphenate",
        "hyphenate-character",
        "-webkit-hyphenate-character",
        "hyphenate-limit-chars",
        "-webkit-hyphenate-limit-before",
        "-webkit-hyphenate-limit-after",
        "-webkit-hyphenate-limit-lines",
    }
)
"""The hyphenation group of properties, removed whole (FR-NORM-16)."""

ALIGNMENT_PROPERTIES: frozenset[str] = frozenset({"text-align", "text-align-last"})

BODY_ALIGNMENTS: frozenset[str] = frozenset(
    {"justify", "left", "start", "auto", "inherit", "initial", "unset"}
)
"""Alignment values that only set body-text alignment, the reader's own setting (FR-NORM-9)."""

FIXED_SIZE_PROPERTIES: frozenset[str] = frozenset(
    {
        "width",
        "height",
        "min-width",
        "min-height",
        "max-height",
        "inline-size",
        "block-size",
        "min-inline-size",
        "min-block-size",
        "max-block-size",
    }
)
"""Removed when absolute, outside image contexts (FR-NORM-6). ``max-width`` and
``max-inline-size`` are not here: they are removed only on the root (ROOT_LAYOUT_PROPERTIES)."""

ROOT_LAYOUT_PROPERTIES: frozenset[str] = frozenset(
    {
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "margin-block",
        "margin-block-start",
        "margin-block-end",
        "margin-inline",
        "margin-inline-start",
        "margin-inline-end",
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "padding-block",
        "padding-block-start",
        "padding-block-end",
        "padding-inline",
        "padding-inline-start",
        "padding-inline-end",
        "width",
        "min-width",
        "max-width",
        "inline-size",
        "min-inline-size",
        "max-inline-size",
        "column-count",
        "column-width",
        "columns",
        "column-gap",
        "-webkit-column-count",
        "-webkit-column-width",
        "-webkit-columns",
        "-webkit-column-gap",
        "-epub-column-count",
        "-epub-column-width",
        "-epub-columns",
        "-epub-column-gap",
    }
)
"""Removed whatever their value, on root contexts only (FR-NORM-17)."""

PAGE_MARGIN_DESCRIPTORS: frozenset[str] = frozenset(
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
    }
)
"""The ``@page`` descriptors removed with the page layout (FR-NORM-17)."""

BOX_PROPERTIES: frozenset[str] = frozenset(
    {
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "margin-block",
        "margin-block-start",
        "margin-block-end",
        "margin-inline",
        "margin-inline-start",
        "margin-inline-end",
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "padding-block",
        "padding-block-start",
        "padding-block-end",
        "padding-inline",
        "padding-inline-start",
        "padding-inline-end",
        "text-indent",
    }
)
"""Absolute lengths here convert to ``em``, never removed (FR-NORM-8)."""

SEMANTIC_PROPERTIES: frozenset[str] = frozenset(
    {
        "font-weight",
        "font-style",
        "font-variant",
        "font-variant-caps",
        "text-decoration",
        "text-decoration-line",
        "text-transform",
    }
)
"""What a drop cap keeps at the ``strict`` level (FR-NORM-10)."""

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
