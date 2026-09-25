"""Structural self-check for merge plans.

This module enforces the merge's own invariants (SH-1…SH-21) over the merge plan
before it is written to disk. These checks verify the plan's internal consistency
(no duplicate IDs, referential integrity, valid XML names, etc.) and ensure that
every manifest entry has content and every content file is accounted for.

**Out of scope:** epubcheck and deep EPUB validation become a separate plugin.
This module only checks the invariants the merge itself can verify from its plan.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from ebookerr_sdk.epub.builder import TocEntry
from ebookerr_sdk.epub.roles import NON_CHAPTER_ITEM_IDS, TITLE_PAGE_ITEM_IDS

from epub_merge.merge.errors import MergeContentError, MergeStructureError
from epub_merge.merge.plan import MergePlan

logger = logging.getLogger(__name__)

# CHC-D1: a title page counts as a chapter (role TITLE, not OTHER) — only the genuinely
# structural non-chapter ids (cover, nav, toc, ncx, log_page, ...) are excluded here.
_STRUCTURAL_NON_CHAPTER_ITEM_IDS = NON_CHAPTER_ITEM_IDS - TITLE_PAGE_ITEM_IDS

# XML NCName pattern: must start with letter or underscore, then alphanumeric/dot/underscore/dash
NCNAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9._\-]*$")


def _check_uniqueness(plan: MergePlan) -> None:
    """Check uniqueness constraints: manifest IDs, hrefs, spine idrefs, TOC hrefs.

    Raises MergeStructureError if any duplicates are found.

    Args:
        plan: The merge plan to validate.

    Raises:
        MergeStructureError: If duplicates are detected.
    """

    def _check_unique_values(values: list[str], error_template: str) -> None:
        """Helper to check if all values are unique."""
        seen = set()
        for value in values:
            if value in seen:
                raise MergeStructureError(error_template.format(value))
            seen.add(value)

    _check_unique_values(
        [entry.item_id for entry in plan.package.manifest],
        'duplicate manifest id "{}"',
    )
    _check_unique_values(
        [entry.href for entry in plan.package.manifest],
        'duplicate manifest href "{}"',
    )
    _check_unique_values(
        [entry.idref for entry in plan.package.spine],
        'duplicate spine idref "{}"',
    )
    _check_unique_values(
        _collect_toc_hrefs(plan.toc),
        'duplicate TOC href "{}"',
    )


def _collect_toc_hrefs(toc: Sequence[TocEntry]) -> list[str]:
    """Collect all hrefs from a nested TOC structure (including children).

    Args:
        toc: A sequence of TocEntry objects.

    Returns:
        A flat list of all href values found in the TOC and its children.
    """
    hrefs = []
    for entry in toc:
        hrefs.append(entry.href)
        if entry.children:
            hrefs.extend(_collect_toc_hrefs(entry.children))
    return hrefs


def _check_references(plan: MergePlan) -> None:
    """Check referential integrity: spine idrefs and TOC hrefs must exist in manifest.

    Raises MergeStructureError if any references are broken.

    Args:
        plan: The merge plan to validate.

    Raises:
        MergeStructureError: If a spine idref or TOC href doesn't match manifest.
    """
    manifest_ids = {entry.item_id for entry in plan.package.manifest}
    manifest_hrefs = {entry.href for entry in plan.package.manifest}

    # Check spine idrefs
    for spine_entry in plan.package.spine:
        if spine_entry.idref not in manifest_ids:
            raise MergeStructureError(
                f'spine references unknown manifest item "{spine_entry.idref}"'
            )

    # Check TOC hrefs
    toc_hrefs = _collect_toc_hrefs(plan.toc)
    for href in toc_hrefs:
        if href not in manifest_hrefs:
            raise MergeStructureError(f'TOC entry references unknown manifest href "{href}"')


def _check_names(plan: MergePlan) -> None:
    """Check valid XML NCNames and no 'merged' substring.

    Raises MergeStructureError if any IDs are invalid or contain 'merged'.

    Args:
        plan: The merge plan to validate.

    Raises:
        MergeStructureError: If invalid names are found.
    """

    def _check_ncname_and_merged(value: str, error_type: str) -> None:
        """Check if value is valid NCName and doesn't contain 'merged'."""
        if "manifest id" in error_type and not NCNAME_PATTERN.match(value):
            raise MergeStructureError(f'{error_type} "{value}" is not a valid XML name')
        if "merged" in value.lower():
            raise MergeStructureError(f'{error_type} "{value}" contains "merged"')

    for entry in plan.package.manifest:
        _check_ncname_and_merged(entry.item_id, "manifest id")
        _check_ncname_and_merged(entry.href, "manifest href")

    for spine_entry in plan.package.spine:
        _check_ncname_and_merged(spine_entry.idref, "spine idref")

    for href in _collect_toc_hrefs(plan.toc):
        _check_ncname_and_merged(href, "TOC href")

    for key in plan.files:
        _check_ncname_and_merged(key, "file key")


def _check_xhtml_flat(plan: MergePlan) -> None:
    """Check XHTML entries have no subdirectories (no `/` in href).

    Raises MergeStructureError if any XHTML entry has a `/` in its href.

    Args:
        plan: The merge plan to validate.

    Raises:
        MergeStructureError: If an XHTML entry has a subdirectory.
    """
    for entry in plan.package.manifest:
        if entry.media_type == "application/xhtml+xml" and "/" in entry.href:
            raise MergeStructureError(f'XHTML manifest entry "{entry.href}" must be flat (no `/`)')


def _check_files(plan: MergePlan) -> None:
    """Check completeness: every manifest entry (except toc.ncx/nav.xhtml) has content.

    Also verify every file is accounted for in the manifest.

    Raises MergeStructureError if mismatches are found.

    Args:
        plan: The merge plan to validate.

    Raises:
        MergeStructureError: If manifest/file mismatches are found.
    """
    manifest_hrefs = {entry.href for entry in plan.package.manifest}
    files_keys = set(plan.files.keys())

    # Every manifest href (except toc.ncx and nav.xhtml) must be in files
    for href in manifest_hrefs:
        if href not in ("toc.ncx", "nav.xhtml") and href not in files_keys:
            raise MergeStructureError(f'manifest entry "{href}" has no file')

    # Every file key must be in the manifest
    for key in files_keys:
        if key not in manifest_hrefs:
            raise MergeStructureError(f'file "{key}" is not in the manifest')


def _check_count(plan: MergePlan, expected_chapters: int) -> None:
    """Check that the plan has at least the expected number of chapters.

    Structurally non-chapter spine entries (cover, nav, toc, ncx, log_page, ...) don't
    count — but a title page does (``CHC-D1``): it carries role ``TITLE``, not ``OTHER``,
    the same rule :func:`~epub_merge.merge.merge._content_chapter_indices` used to
    compute ``expected_chapters``.

    Args:
        plan: The merge plan to validate.
        expected_chapters: The minimum number of chapters expected.

    Raises:
        MergeContentError: If the plan has too few chapters — the merge did not
            account for every input chapter.
    """
    manifest_id_to_entry = {entry.item_id: entry for entry in plan.package.manifest}

    # Count spine entries that are actual chapters
    chapter_count = 0
    for spine_entry in plan.package.spine:
        manifest_entry = manifest_id_to_entry.get(spine_entry.idref)
        if manifest_entry is None:
            continue
        # Skip structurally non-chapter items (a title page still counts)
        if spine_entry.idref.lower() in _STRUCTURAL_NON_CHAPTER_ITEM_IDS:
            continue
        # Count this as a chapter
        chapter_count += 1

    if chapter_count < expected_chapters:
        raise MergeContentError(
            f"merge plan has {chapter_count} chapter(s), expected at least {expected_chapters}"
        )


def validate_plan(plan: MergePlan, *, expected_chapters: int) -> None:
    """Verify the merge plan's structural invariants; raise on the first violation.

    Enforces:
    - SH-4 / MR-DEDUP-1: manifest IDs, hrefs, spine idrefs, TOC hrefs are unique
    - Referential integrity: spine idrefs and TOC hrefs exist in manifest
    - MR-ID-1 / ACC-ID-1: manifest IDs are valid XML NCNames
    - MR-FLAT-1: XHTML entries have flat hrefs (no `/`)
    - MR-CLEAN-1 / SH-2: no 'merged' substring in any ID, href, or file key
    - Completeness: every manifest entry (except toc.ncx/nav.xhtml) has content
    - Content reconciliation: plan has at least expected_chapters chapters

    Args:
        plan: The merge plan to validate.
        expected_chapters: The minimum number of chapters expected in the plan.

    Raises:
        MergeStructureError: If a structural invariant (uniqueness, referential
            integrity, valid names, flat XHTML, or file completeness) is
            violated; includes the violated invariant and the offending value
            in the message.
        MergeContentError: If the plan accounts for fewer chapters than
            ``expected_chapters`` — content reconciliation failed.
    """
    # Run checks in order
    _check_uniqueness(plan)
    _check_references(plan)
    _check_names(plan)
    _check_xhtml_flat(plan)
    _check_files(plan)
    _check_count(plan, expected_chapters)

    # Log success
    logger.debug(
        "Merge plan validated: %d manifest entry(ies), %d spine entry(ies), %d file(s)",
        len(plan.package.manifest),
        len(plan.package.spine),
        len(plan.files),
    )
