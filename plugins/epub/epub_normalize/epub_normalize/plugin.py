"""EpubNormalizePlugin — remove publisher styling so a reader's own settings apply.

A reading application lets its user choose a font, colours, a text size, line spacing,
justification, hyphenation and page margins, but a publisher's stylesheet that sets those
itself wins in most readers, so the user's settings appear to do nothing — the single most
common complaint about self-published and site-exported EPUBs. The normalizer removes that
styling at the level the user picks (`FR-NORM-15`); the book's structure and semantic
formatting — headings, paragraphs, line and paragraph breaks, blank lines, bold, italic,
small caps, lists, tables, images, SVG and MathML — stay (`FR-NORM-1`).

**Levels** — the plugin's one setting, each level a set of rule groups (`TR-NORM-8`):
- ``light`` (colours and fonts only): colours (`FR-NORM-4`), font families, `@font-face`
  rules and the embedded font files nothing references any more (`FR-NORM-3`,
  `FR-NORM-11`). A monospace family is kept.
- ``standard`` (everything your reader can set, the default): the light groups, plus
  font sizes (`FR-NORM-7`) — body text takes the reader's size, others become relative —
  line, letter and word spacing (`FR-NORM-5`), hyphenation (`FR-NORM-16`), body-text
  alignment (`FR-NORM-9`; centred and right-aligned text stay), page layout (`FR-NORM-17`:
  root margins, paddings and widths, `@page` margins, Adobe page templates, the legacy
  `<body>` margin attributes), fixed dimensions (`FR-NORM-6`) and fixed margins and indents
  (`FR-NORM-8`, made relative).
- ``strict`` (everything but headings and emphasis): the standard groups, but only
  headings keep a size and drop caps lose everything except emphasis (`FR-NORM-10`).

**The paragraph rule** (`FR-NORM-18`): paragraph separation is the book's own, so blank
lines are kept and marked; a blank line is never removed.

**Exemptions**: SVG, vector wrappers and MathML contexts, images and covers keep their
dimensions; `<meta name="viewport">` is removed from reflowable books; fixed-layout books
(`rendition:layout` `pre-paginated`, an OPF fixed-layout meta, or Apple's display options
declaring `fixed-layout` true — `FR-NORM-12`) and DRM-protected books (`FR-NORM-19`) are
never modified and are reported to the run as skips.

**Idempotency** (`TR-NORM-3`, `TR-NORM-5`): each rewritten file carries a marker with a
12-hex signature of the ruleset version and the rule groups, so a level change invalidates
every marker, and a no-op run leaves the staged EPUB byte-identical (the core publishes it
iff bytes changed, so a gratuitous rewrite would falsely re-cascade an `EpubModified`
event). Choosing a lighter level does not restore styling a heavier level already removed.

Prior art:
- **Calibre** — `transform_styles` / "Remove specified style information" filter:
  property-level strip list and the book's base font size that relative sizes are
  measured against.
- **Readium CSS** — the user settings (`--USER__*`) model: they only take effect where the
  publisher has not already set the property (why stripping is necessary).
- **KOReader** — style tweaks: per-property overrides of the publisher's stylesheet.
- **EPUB 3.3** — the `rendition:layout` property identifies fixed-layout books.
- **Apple Books** — `com.apple.ibooks.display-options.xml` declares a fixed-layout book.
- **Komga / Kavita** — reflowable rendering is what a fixed width breaks.
- **tinycss2** (WeasyPrint lineage) — tokenizer/serializer chosen for round-trip fidelity
  over regex.

Pipeline position: `priority=70` — after `epub_chapter_reorder` (55) and before
`epub_validate` (950). The core book-details step runs before every EPUB plugin in the pass.
Chapters are reordered before styling is neutralised.

The plugin runs both unattended (on `EpubCreated`/`EpubModified` events) and on demand
via the **Normalize** book-selection action. It returns no `BookPatch` because it
changes only the file.

Requirement IDs: FR-NORM-1 through FR-NORM-20, TR-NORM-1 through TR-NORM-8.
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
    SettingsSchema,
)
from ebookerr_sdk.spi.manifest import package_manifest

from epub_normalize.normalize import (
    DEFAULT_LEVEL,
    LEVELS,
    NormalizeOptions,
    normalize_epub,
    options_for_level,
)
from epub_normalize.normalize.normalize import DRM_REASON, FIXED_LAYOUT_REASON

logger = logging.getLogger(__name__)


FIXED_LAYOUT_SKIP = "fixed-layout book"
"""The skip a fixed-layout book reports to the run: page layouts are left alone (EXP-395)."""

DRM_SKIP = "DRM-protected book"
"""The skip a DRM-protected book reports to the run: it is never modified (FR-NORM-19)."""

UNREADABLE_FAILURE = "the EPUB file could not be read — it may be damaged"
"""The failure an unreadable book reports to the run; trying again cannot help (EXP-395)."""


def _log_outcome(report: Any, item: EpubItem, options: NormalizeOptions, elapsed: float) -> None:
    """Log one book's normalization outcome at the appropriate level.

    Args:
        report: The NormalizeReport returned by normalize_epub.
        item: The EpubItem that was processed.
        options: The options the run used (its level is logged).
        elapsed: Elapsed time in seconds.
    """
    title = item.book.title or "(unknown title)"

    if report.skipped_reason == FIXED_LAYOUT_REASON:
        logger.warning(
            'Normalize skipped for "%s" (book_id=%s): fixed-layout book',
            title,
            item.book.book_id,
        )
    elif report.skipped_reason == DRM_REASON:
        logger.warning(
            'Normalize skipped for "%s" (book_id=%s): DRM-protected book',
            title,
            item.book.book_id,
        )
    elif report.changed:
        logger.info(
            'Normalized "%s" (book_id=%s) at level %s: removed %d declaration(s), %d rule(s), '
            "%d inline style(s), %d HTML attribute(s), %d font file(s), %d page template(s); "
            "converted %d value(s); marked %d blank line(s); "
            "across %d CSS + %d chapter file(s) in %.2fs",
            title,
            item.book.book_id,
            options.level,
            report.counts.declarations_removed,
            report.counts.rules_removed,
            report.counts.style_attributes_removed,
            report.counts.html_attributes_removed,
            report.counts.font_files_removed,
            report.counts.page_templates_removed,
            report.counts.declarations_converted,
            report.counts.blank_lines_marked,
            report.counts.css_files_changed,
            report.counts.xhtml_files_changed,
            elapsed,
        )
    else:
        logger.info(
            'Normalize no-op for "%s" (book_id=%s) at level %s: nothing to change, '
            "%d file(s) already normalized, in %.2fs",
            title,
            item.book.book_id,
            options.level,
            report.counts.files_skipped_marker,
            elapsed,
        )


class EpubNormalizePlugin:
    """Strip publisher styling from EPUB so reader themes work.

    Runs unattended on `EpubCreated`/`EpubModified` events and on demand via the
    **Normalize** book-selection action. Returns no `BookPatch` because it changes
    only the file; a no-op run leaves the file byte-identical.
    """

    manifest = package_manifest(__file__)

    def settings_schema(self) -> SettingsSchema:
        """Return the settings schema for this plugin.

        Returns:
            The settings schema.
        """
        return self.manifest.settings_schema

    def _options(self, settings: Mapping[str, Any]) -> NormalizeOptions:
        """Build the rule groups of the stored styling level.

        Args:
            settings: The resolved settings from the context.

        Returns:
            The NormalizeOptions of the stored level; an unknown level is read as the default
            level, with one WARNING.
        """
        level = str(settings.get("level", DEFAULT_LEVEL)).strip().lower()
        try:
            return options_for_level(level)
        except ValueError:
            logger.warning(
                "Normalize level %r is not one of %s; using %s",
                settings.get("level"),
                ", ".join(LEVELS),
                DEFAULT_LEVEL,
            )
            return options_for_level(DEFAULT_LEVEL)

    def process(self, items: tuple[EpubItem, ...], ctx: PluginContext) -> list[BookPatch]:
        """Normalize all EPUBs in the batch.

        Args:
            items: The EPUBs to normalize.
            ctx: The plugin context.

        Returns:
            An empty list (changes are detected by file diffing). A fixed-layout or
            DRM-protected book is reported to the run as a skip, an unreadable one as a
            failure that is not retryable, so the run's counts say what happened to every
            book (EXP-395).
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
                    "Normalize failed for %s (malformed EPUB)",
                    item.epub_path,
                    exc_info=True,
                )
                ctx.report_failure(item.book.book_id, UNREADABLE_FAILURE, retryable=False)
                ctx.report((i + 1) / total * 100.0 if total else 100.0)
                continue

            elapsed = time.monotonic() - started
            _log_outcome(report, item, options, elapsed)
            if report.skipped_reason == FIXED_LAYOUT_REASON:
                ctx.report_skip(item.book.book_id, FIXED_LAYOUT_SKIP)
            elif report.skipped_reason == DRM_REASON:
                ctx.report_skip(item.book.book_id, DRM_SKIP)
            ctx.report((i + 1) / total * 100.0 if total else 100.0)

        return []
