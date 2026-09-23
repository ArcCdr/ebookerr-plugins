"""EpubNormalizePlugin — strip layout-interfering styling so reader themes work.

A publisher's EPUB commonly hardcodes the font family, the text and background colour,
the line height and an absolute font size. A reading application's own font, theme and
font-size controls are implemented as CSS the publisher's rules then override, so the
user's settings appear to do nothing — the single most common complaint about
self-published and site-exported EPUBs. Readium CSS makes this explicit: its `--USER__*`
custom properties only take effect where the publisher has not already set the property.

The normalizer applies three transformations:

**STRIP** — properties removed completely:
- `font-family` and the `font` shorthand (blocks the reader's font choice).
- `@font-face` rules and embedded font files left unreferenced afterward (dead weight,
  200 KB–2 MB/book).
- `color` and `background-color` (hardcoded near-black on near-white is unreadable in
  dark themes).
- The `background` shorthand, but only when the value contains no `url()` (when it
  carries an image it is a real asset reference).
- `line-height`, `letter-spacing`, `word-spacing` (override the reader's spacing
  controls and break at large user font sizes).
- Absolute `width`, `height`, `min-width`, `min-height`, `max-height` (fixed
  text-container widths cause horizontal scrolling on a phone). `max-width` is never
  removed: it is the responsive constraint, not the problem.
- `<meta name="viewport">` in reflowable books (a fixed viewport fights the reader's
  pagination).

**TRANSFORM** — converted to relative/flexible:
- Absolute `font-size` → `em` on a 16 px base (the CSS initial value and every
  mainstream reading system's default); `em` not `rem` because `rem` support is
  unreliable in older EPUB 2 readers. On `html`/`body` selectors, stripped outright
  instead of converted (that value *is* the reader's base size). Clamped
  `[0.4em, 4em]` to stop a `48pt` display heading becoming a four-line monster.
- Absolute margins, paddings and `text-indent` → `em`, but only when margin
  normalization is enabled (off by default: the riskiest transform).

**PRESERVE** — never touched:
- `text-align` and `text-indent` (centring is meaningful in poetry, signatures,
  epigraphs and chapter headings; indent is typographic intent).
- Drop caps / initial caps (whole rule exempt from every strip and transform).
- SVG, vector wrappers and MathML contexts (dimensions preserved on images/covers).
- Fixed-layout books declaring `rendition:layout = pre-paginated` (comics and
  illustrated children's books depend on the styling this removes).

Edge cases handled:

- **Drop caps and initial caps** — rules matching `::first-letter`, `:first-letter`,
  `::first-line`, `dropcap`, `drop-cap`, `initial-cap` or `first-letter` are exempt.
- **SVG title pages and vector wrappers** — rules matching `svg`, `image` or `math`
  are exempt; SVG members are never rewritten.
- **Covers and images** — dimension stripping skips rules matching `img`, `svg`,
  `image`, `figure`, `.cover*` or `.titlepage*`. `.covert` matching `.cover` is a
  deliberate, pinned over-match: preserving is the safe direction.
- **Malformed XHTML** — chapters are rewritten by targeted text substitution, never
  XML round-trip, so a mismatched tag or bare `&nbsp;` is still cleaned. A before/after
  well-formedness probe reverts a rewrite that would newly break a chapter.
- **CSS commented out inside `<style>`** — a block beginning with `<!--` is left
  verbatim (already inert).
- **Publisher hacks that must survive** — `@page`, `@import`, `@charset`, `@namespace`
  and unknown at-rules pass through untouched.
- **Obfuscated fonts** — deleting a font prunes its `META-INF/encryption.xml` entry
  (Adobe/IDPF obfuscation).

Idempotency is tracked per-file by a marker (CSS comment on stylesheets, XML comment
on chapters) carrying a 12-hex-character signature of the ruleset version plus the
effective options. A settings change invalidates every marker automatically. A no-op
run leaves the staged EPUB byte-identical (the core publishes it iff bytes changed,
so a gratuitous rewrite would falsely re-cascade an `EpubModified` event).

Prior art:
- **Calibre** — `transform_styles` / "Remove specified style information" filter:
  property-level strip list and absolute→relative font sizing against a base size.
- **Readium CSS** — the `--USER__*` override model (why stripping is necessary).
- **Pandoc** — deliberately minimal EPUB CSS output as the target state.
- **Komga / Kavita** — reflowable rendering is what a fixed width breaks.
- **EPUB 2.0.1 OPS §3.2** and **EPUB 3.3 Content Documents** — CSS subset support
  and the `rendition:layout` property that identifies fixed-layout books.
- **tinycss2** (WeasyPrint lineage) — tokenizer/serializer chosen for round-trip
  fidelity over regex.

Pipeline position: `priority=70` — after `epub_chapter_url` (60) and before
`epub_chapter_reorder` (100). The core book-details step runs before every EPUB plugin in the
pass. Styling is neutralised before chapters are reordered.

The plugin runs both unattended (on `EpubCreated`/`EpubModified` events) and on demand
via the **Normalize** book-selection action. It returns no `BookPatch` because it
changes only the file; a no-op run leaves the file byte-identical.

Requirement IDs: FR-NORM-1 through FR-NORM-14, TR-NORM-1 through TR-NORM-5.
"""

from __future__ import annotations

import logging
import time
import zipfile
from collections.abc import Mapping
from typing import Any

from ebookerr_sdk.epub import EpubError
from ebookerr_sdk.spi import (
    BookPatch,
    EpubItem,
    PluginContext,
    PluginEventType,
    PluginManifest,
    PluginType,
    SettingsField,
    SettingsSchema,
    UiTrigger,
)

from epub_normalize.normalize import NormalizeOptions, normalize_epub
from epub_normalize.normalize.normalize import FIXED_LAYOUT_REASON

logger = logging.getLogger(__name__)

_SETTING_KEYS: tuple[str, ...] = (
    "strip_fonts",
    "strip_colors",
    "convert_font_sizes",
    "strip_line_spacing",
    "strip_fixed_dimensions",
    "preserve_alignment",
    "normalize_margins",
)

_SETTINGS_SCHEMA = SettingsSchema(
    fields=(
        SettingsField(
            key="strip_fonts",
            type="bool",
            label="Strip embedded fonts",
            default=True,
            help="Remove font-family declarations and @font-face rules, and delete the "
            "embedded font files nothing points at any more, so your reader's own font "
            "choice applies.",
        ),
        SettingsField(
            key="strip_colors",
            type="bool",
            label="Strip hardcoded colours",
            default=True,
            help="Remove text and background colours the publisher fixed, so a dark or "
            "sepia reading theme works.",
        ),
        SettingsField(
            key="convert_font_sizes",
            type="bool",
            label="Convert absolute font sizes",
            default=True,
            help="Rewrite font sizes given in px/pt/cm as relative em values, and drop the "
            "size fixed on the whole document, so your reader's font-size control works.",
        ),
        SettingsField(
            key="strip_line_spacing",
            type="bool",
            label="Strip fixed line and letter spacing",
            default=True,
            help="Remove line-height, letter-spacing and word-spacing, which override your "
            "reader's own spacing controls.",
        ),
        SettingsField(
            key="strip_fixed_dimensions",
            type="bool",
            label="Strip fixed widths and heights",
            default=True,
            help="Remove absolute widths and heights on text containers, which cause "
            "sideways scrolling on a phone. Images and covers are never touched.",
        ),
        SettingsField(
            key="preserve_alignment",
            type="bool",
            label="Preserve text alignment",
            default=True,
            help="Keep text-align and text-indent, which carry real meaning in poetry, "
            "signatures and headings. Turn this off to strip them too.",
        ),
        SettingsField(
            key="normalize_margins",
            type="bool",
            label="Convert fixed margins and padding",
            default=False,
            help="Also rewrite absolute margins and padding as relative em values. Off by "
            "default: it is the change most likely to alter a book's intended layout.",
        ),
    )
)

_MANIFEST = PluginManifest(
    id="epub_normalize",
    name="EPUB Normalize",
    version="1.1.0",
    plugin_type=PluginType.EPUB,
    settings_schema=_SETTINGS_SCHEMA,
    headless=True,
    headed=True,
    priority=70,
    events=(PluginEventType.EPUB_CREATED, PluginEventType.EPUB_MODIFIED),
    ui_triggers=(
        UiTrigger(
            scope="book_selection_action",
            icon="sweep",
            label="Normalize styling",
            description=(
                "Remove publisher CSS that overrides your reader's font, theme and text size."
            ),
            min_books=1,
        ),
    ),
    description="Strip publisher styling that stops your reader applying its own fonts, "
    "colours, spacing and margins.",
    default_enabled=True,
    run_timeout_s=600,
    icon="format_paint",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_normalize",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_normalize",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
)


def _log_outcome(report: Any, item: EpubItem, elapsed: float) -> None:
    """Log one book's normalization outcome at the appropriate level.

    Args:
        report: The NormalizeReport returned by normalize_epub.
        item: The EpubItem that was processed.
        elapsed: Elapsed time in seconds.
    """
    title = item.book.title or "(unknown title)"

    if report.skipped_reason == FIXED_LAYOUT_REASON:
        logger.warning(
            'Normalize skipped for "%s" (book_id=%s): fixed-layout book',
            title,
            item.book.book_id,
        )
    elif report.changed:
        logger.info(
            'Normalized "%s" (book_id=%s): removed %d declaration(s), %d rule(s), '
            "%d inline style(s), %d font file(s); converted %d value(s) across "
            "%d CSS + %d chapter file(s) in %.2fs",
            title,
            item.book.book_id,
            report.counts.declarations_removed,
            report.counts.rules_removed,
            report.counts.style_attributes_removed,
            report.counts.font_files_removed,
            report.counts.declarations_converted,
            report.counts.css_files_changed,
            report.counts.xhtml_files_changed,
            elapsed,
        )
    else:
        logger.info(
            'Normalize no-op for "%s" (book_id=%s): nothing to change, '
            "%d file(s) already normalized, in %.2fs",
            title,
            item.book.book_id,
            report.counts.files_skipped_marker,
            elapsed,
        )


class EpubNormalizePlugin:
    """Strip publisher styling from EPUB so reader themes work.

    Runs unattended on `EpubCreated`/`EpubModified` events and on demand via the
    **Normalize** book-selection action. Returns no `BookPatch` because it changes
    only the file; a no-op run leaves the file byte-identical.
    """

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the settings schema for this plugin.

        Returns:
            The settings schema.
        """
        return _SETTINGS_SCHEMA

    def _options(self, settings: Mapping[str, Any]) -> NormalizeOptions:
        """Build NormalizeOptions from resolved settings.

        Args:
            settings: The resolved settings from the context.

        Returns:
            A NormalizeOptions object with settings applied.
        """
        defaults = NormalizeOptions()
        return NormalizeOptions(
            **{key: bool(settings.get(key, getattr(defaults, key))) for key in _SETTING_KEYS}
        )

    def process(self, items: tuple[EpubItem, ...], ctx: PluginContext) -> list[BookPatch]:
        """Normalize all EPUBs in the batch.

        Args:
            items: The EPUBs to normalize.
            ctx: The plugin context.

        Returns:
            An empty list (changes are detected by file diffing).
        """
        options = self._options(ctx.settings)
        total = len(items)

        for i, item in enumerate(items):
            ctx.check_cancelled()
            started = time.monotonic()

            try:
                report = normalize_epub(item.epub_path, options=options)
            except (EpubError, zipfile.BadZipFile):
                logger.warning(
                    "Normalize skipped for %s (malformed EPUB)",
                    item.epub_path,
                    exc_info=True,
                )
                ctx.report((i + 1) / total * 100.0 if total else 100.0)
                continue

            elapsed = time.monotonic() - started
            _log_outcome(report, item, elapsed)
            ctx.report((i + 1) / total * 100.0 if total else 100.0)

        return []
