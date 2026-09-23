"""EpubChapterUrlPlugin — stamp chapter URLs into staged EPUBs.

Declares a ``<meta name="chapterurl">`` on every staged EPUB chapter that lacks one,
applying the same policy as the merge pipeline:

* **One-chapter book** — that chapter *is* the book, so the book's own URL is stamped
  onto it.
* **Multi-chapter book** — if any chapter declares a URL, every chapter is left exactly
  as it is (the per-chapter URLs are real and must be preserved). Only when *none* of
  them declares one is the book URL stamped onto all of them, so the merged book still
  points somewhere meaningful.

Filling individual gaps in a partially-annotated book is deliberately not done: that
chapter is not at the book URL, and guessing would record a wrong address.
"""

from __future__ import annotations

import logging
import zipfile

from ebookerr_sdk.epub import EpubDocument, EpubError
from ebookerr_sdk.spi import (
    BookPatch,
    ChapterLink,
    EpubItem,
    PluginContext,
    PluginEventType,
    PluginManifest,
    PluginType,
    SettingsSchema,
)

logger = logging.getLogger(__name__)

_MANIFEST = PluginManifest(
    id="epub_chapter_url",
    name="Chapter URL Stamp",
    description=(
        "Records each chapter's own web address inside the EPUB, so a chapter can be "
        "traced back to the page it came from."
    ),
    version="1.1.0",
    plugin_type=PluginType.EPUB,
    settings_schema=SettingsSchema(),
    headless=True,
    priority=60,
    events=(PluginEventType.EPUB_CREATED, PluginEventType.EPUB_MODIFIED),
    default_enabled=True,
    run_timeout_s=600,
    icon="link",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_chapter_url",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_chapter_url",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
)


class EpubChapterUrlPlugin:
    """Declare a chapter URL inside every staged EPUB chapter that lacks one."""

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the (empty) settings schema — this plugin has no user-configurable options."""
        return SettingsSchema()

    def process(self, items: tuple[EpubItem, ...], ctx: PluginContext) -> list[BookPatch]:
        """Stamp chapter URLs on each staged EPUB and report progress across the batch.

        Args:
            items: Staged EPUB items to stamp in place.
            ctx: Plugin context; used for cancellation checks and progress reporting.

        Returns:
            One ``BookPatch(chapters=(...))`` per EPUB whose chapter index was not already
            current; already-indexed EPUBs contribute nothing. A malformed EPUB or one
            with no story_url is skipped (logged) rather than failing the whole batch.
        """
        patches: list[BookPatch] = []
        total = len(items)

        for i, item in enumerate(items):
            ctx.check_cancelled()
            patch = self._stamp_item(item)
            if patch:
                patches.append(patch)
            ctx.report((i + 1) / total * 100.0 if total else 100.0)

        return patches

    def _stamp_item(self, item: EpubItem) -> BookPatch | None:
        """Stamp chapter URLs on one EPUB; return a patch if the index changed.

        Args:
            item: The EPUB item to process.

        Returns:
            A BookPatch to declare the newly-stamped URL, or None if no change.
        """
        # Skip if no story URL
        if not item.book.story_url:
            logger.debug(
                "Chapter-URL stamp skipped for book_id=%s: no story URL",
                item.book.book_id,
            )
            return None

        # Open the EPUB
        try:
            doc = EpubDocument.open(item.epub_path)
        except (EpubError, zipfile.BadZipFile):
            logger.warning(
                "Chapter-URL stamp skipped for %s (malformed EPUB)",
                item.epub_path,
                exc_info=True,
            )
            return None

        # Collect content chapter hrefs
        content_hrefs = self._collect_content_hrefs(doc)
        if not content_hrefs:
            return None

        # Read declared URLs for each chapter
        declared_urls = self._read_declared_urls(doc, content_hrefs)

        # Decide which chapters to stamp
        chapters_to_stamp = self._decide_chapters_to_stamp(content_hrefs, declared_urls)

        # Apply stamping
        stamped_count = self._apply_stamping(doc, chapters_to_stamp, item.book.story_url)

        # Save and log
        if stamped_count > 0:
            doc.save()
            logger.info(
                'Chapter URL stamped on %d chapter(s) of "%s" (book_id=%s): %s',
                stamped_count,
                item.book.title or "(unknown title)",
                item.book.book_id,
                item.book.story_url,
            )

            # Only emit patch if the URL is not already indexed
            if not any(link.url == item.book.story_url for link in item.book.chapters):
                return BookPatch(
                    book_id=item.book.book_id,
                    chapters=(ChapterLink(url=item.book.story_url, title=None),),
                )
        else:
            logger.debug("Chapter URLs already declared for book_id=%s", item.book.book_id)

        return None

    def _collect_content_hrefs(self, doc: EpubDocument) -> list[str]:
        """Collect the hrefs of the EPUB's content chapters, in spine order.

        Delegates to :meth:`~ebookerr_sdk.epub.document.EpubDocument.content_chapters`
        so the plugin stamps exactly the documents the rest of the app counts as
        chapters — the title page, cover, log page, nav document and ``linear="no"``
        entries are excluded there, once.
        """
        return [item.href for item in doc.content_chapters()]

    def _read_declared_urls(
        self, doc: EpubDocument, content_hrefs: list[str]
    ) -> dict[str, str | None]:
        """Read the chapterurl meta from each chapter."""
        declared_urls: dict[str, str | None] = {}
        for href in content_hrefs:
            try:
                chapter = doc.chapter(href)
                declared_urls[href] = chapter.meta("chapterurl")
            except Exception:  # noqa: BLE001
                declared_urls[href] = None
        return declared_urls

    def _decide_chapters_to_stamp(
        self, content_hrefs: list[str], declared_urls: dict[str, str | None]
    ) -> list[str]:
        """Decide which chapters to stamp based on the two-rule policy."""
        if len(content_hrefs) == 1:
            return content_hrefs
        if len(content_hrefs) > 1:
            # Multiple chapters: stamp all only if none declare a URL
            any_declared = any(declared_urls[href] for href in content_hrefs)
            if not any_declared:
                return content_hrefs
        return []

    def _apply_stamping(self, doc: EpubDocument, chapters_to_stamp: list[str], url: str) -> int:
        """Stamp chapters with the URL; return count of chapters actually stamped."""
        stamped_count = 0
        for href in chapters_to_stamp:
            try:
                chapter = doc.chapter(href)
                if chapter.set_meta("chapterurl", url):
                    doc.write_chapter(href, chapter)
                    stamped_count += 1
            except Exception:  # noqa: BLE001
                logger.debug("Failed to stamp chapter URL on %s", href, exc_info=True)
        return stamped_count
