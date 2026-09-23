"""Plan the resource set for a merged EPUB.

This module handles re-basing, de-duplication, and de-obfuscation of assets
across multiple input books.

Key design decisions:
- Resources keep their nested directory structure (re-based against their own
  book's content root), because flattening them would make navigation harder.
  Only XHTML documents are flattened.
- The survivor (books[0]) claims its assets first, ensuring deterministic
  collision-suffix assignment.
- Fonts are de-obfuscated before hashing to enable deduplication of
  obfuscated copies of the same underlying font (MR-ASSET-2).
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ebookerr_sdk.epub.assets import AssetRegistry, rebase_href
from ebookerr_sdk.epub.fonts import ADOBE_OBFUSCATION, IDPF_OBFUSCATION, deobfuscate

from epub_merge.merge.model import InputBook, InputResource

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AssetPlan:
    """Where every input resource ended up in the merged book.

    Attributes:
        files: Final output path (OPF-relative) to bytes, for every kept resource.
        media_types: Final output path to media type, for the output manifest.
        mapping: ``(book_index, input href)`` to final output path, for reference rewriting.
    """

    files: dict[str, bytes]
    media_types: dict[str, str]
    mapping: dict[tuple[int, str], str]


def plan_assets(
    books: Sequence[InputBook],
    *,
    encryption: Mapping[int, Mapping[str, str]] | None = None,
    registry: AssetRegistry | None = None,
) -> tuple[AssetPlan, AssetRegistry]:
    """Re-base, de-duplicate and de-obfuscate every book's resources into one set.

    Args:
        books: Input books in reading order; books[0] is the survivor.
        encryption: Per-book mapping of resource href to obfuscation algorithm URI.
            Default empty mapping; no decryption is performed.
        registry: A registry to claim into, already carrying any path the caller
            has reserved; a fresh one is created when None.

    Returns:
        A tuple of (AssetPlan, AssetRegistry) describing the merged resource set
        and the registry used to track claimed paths for collision avoidance.
    """
    if encryption is None:
        encryption = {}

    if registry is None:
        registry = AssetRegistry()
    mapping: dict[tuple[int, str], str] = {}
    media_types: dict[str, str] = {}
    survivor_stylesheet = _survivor_stylesheet(books)

    for book in books:
        for resource in book.resources:
            # Rule 2: Skip non-survivor stylesheets
            if resource.media_type == "text/css" and book.index != 0:
                logger.debug(
                    "Merge dropped non-survivor stylesheet: input %d %s", book.index, resource.href
                )
                if survivor_stylesheet:
                    mapping[(book.index, resource.href)] = survivor_stylesheet
                continue

            # Rule 3: De-obfuscate before hashing
            data = _decrypt(resource, book, encryption)

            # Rule 4: Rebase against content root
            preferred = rebase_href(resource.href, book.content_root)

            # Rule 5: Claim the resource
            prior_paths = set(registry.files().keys())
            final = registry.claim(preferred, data)
            mapping[(book.index, resource.href)] = final

            # Rule 6: Record media type for first claimer only
            if final not in media_types:
                media_types[final] = resource.media_type

            # Telemetry
            if final == preferred:
                logger.debug(
                    "Merge asset from input %d: %s -> %s", book.index, resource.href, final
                )
            elif final in prior_paths:
                logger.debug(
                    "Merge asset de-duplicated: input %d %s reuses %s",
                    book.index,
                    resource.href,
                    final,
                )
            else:
                logger.debug(
                    "Merge asset from input %d: %s -> %s", book.index, resource.href, final
                )

    files = registry.files()
    logger.info("Merge planned %d resource file(s) from %d book(s)", len(files), len(books))

    return AssetPlan(files=files, media_types=media_types, mapping=mapping), registry


def _decrypt(
    resource: InputResource, book: InputBook, algorithms: Mapping[int, Mapping[str, str]]
) -> bytes:
    """De-obfuscate a resource if encryption is configured for it.

    Args:
        resource: The resource to potentially decrypt.
        book: The book containing the resource (for identifier and index).
        algorithms: Per-book, per-href encryption algorithm mapping.

    Returns:
        Decrypted bytes if encryption is configured and matches, otherwise
        the input resource data unchanged.
    """
    book_algs = algorithms.get(book.index, {})
    if not book_algs:
        return resource.data

    # Rule 3: Match against both raw href and normalized forms
    algorithm = book_algs.get(resource.href) or book_algs.get(posixpath.basename(resource.href))
    if not algorithm:
        return resource.data

    # De-obfuscate
    data = deobfuscate(resource.data, algorithm=algorithm, unique_identifier=book.identifier)

    # Log if the algorithm is recognized
    if algorithm in (ADOBE_OBFUSCATION, IDPF_OBFUSCATION):
        logger.info(
            "Decrypted obfuscated font: %s (algorithm=%s, input=%d)",
            resource.href,
            algorithm,
            book.index,
        )
    else:
        logger.warning(
            'Unknown font obfuscation algorithm %s for "%s"; writing the file unchanged',
            algorithm,
            resource.href,
        )

    return data


def _survivor_stylesheet(books: Sequence[InputBook]) -> str | None:
    """Find the first stylesheet path of the survivor (books[0]).

    Args:
        books: Input books; books[0] is the survivor.

    Returns:
        The re-based path to the survivor's first stylesheet, or None if the
        survivor has no stylesheets.
    """
    if not books:
        return None

    survivor = books[0]
    if not survivor.stylesheets:
        return None

    first_href = survivor.stylesheets[0]
    return rebase_href(first_href, survivor.content_root)
