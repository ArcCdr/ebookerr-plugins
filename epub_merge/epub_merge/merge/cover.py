"""Plan and render cover images and cover pages for merged EPUBs.

This module handles cover image wiring for merged output books. The cover is sourced
in precedence order:
1. The ``cover_image`` option from MergeOptions (if set)
2. The survivor's existing cover_href (if set and in the resource mapping)
3. None (no cover; no cover files, no guide entry)

The survivor's existing cover image is referenced rather than copied because the
image file itself survives as an ordinary resource in the asset plan, and re-reading
and re-writing it would be redundant.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from ebookerr_sdk.epub.assets import AssetRegistry
from ebookerr_sdk.epub.serialize import element, new_document, pretty_xml

from epub_merge.merge.assets import AssetPlan
from epub_merge.merge.errors import MergeInputError
from epub_merge.merge.model import InputBook, MergeOptions

logger = logging.getLogger(__name__)

COVER_MEDIA_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


@dataclass(frozen=True, slots=True)
class CoverPlan:
    """The merged book's cover wiring.

    Attributes:
        image_path: Output path of the cover image, OPF-relative.
        image_media_type: The image's media type.
        extra_files: New files this plan adds (empty when the image is already
            an output resource).
        page_path: Output path of the cover XHTML wrapper (always
            ``"cover.xhtml"``).
        page_data: The cover wrapper's bytes.
    """

    image_path: str
    image_media_type: str
    extra_files: dict[str, bytes]
    page_path: str
    page_data: bytes


def render_cover_page(image_path: str) -> bytes:
    """Build a minimal, self-contained XHTML wrapper displaying ``image_path``.

    Args:
        image_path: The OPF-relative path to the cover image.

    Returns:
        The serialized XHTML document as UTF-8 bytes.
    """
    doc = new_document("html")
    root = doc.documentElement
    if root is not None:
        root.setAttribute("xmlns", "http://www.w3.org/1999/xhtml")

    head = element(root, "head")  # type: ignore[arg-type]
    element(head, "title", text="Cover")
    element(head, "meta", attrs={"charset": "utf-8"})

    style_text = (
        "@page {padding: 0; margin: 0}\n"
        "body {text-align: center; padding: 0; margin: 0}\n"
        "div {margin: 0; padding: 0}"
    )
    element(head, "style", attrs={"type": "text/css"}, text=style_text)

    body = element(root, "body")  # type: ignore[arg-type]
    div = element(body, "div")
    element(div, "img", attrs={"src": image_path, "alt": "Cover"})

    return pretty_xml(doc)


def plan_cover(
    books: Sequence[InputBook],
    options: MergeOptions,
    assets: AssetPlan,
    *,
    registry: AssetRegistry,
) -> CoverPlan | None:
    """Choose the merged book's cover: the manifest override, else the survivor's, else none.

    Precedence order:
    1. When ``options.cover_image`` is set, read its bytes and determine media type
       from file extension (defaulting to ``"image/jpeg"`` for unknown extensions).
    2. When ``books[0].cover_href`` is set and in ``assets.mapping``, use the mapped
       output path and the survivor's media type.
    3. Otherwise return ``None`` (no cover).

    Args:
        books: Input books in reading order; ``books[0]`` is the survivor.
        options: Merge options including the optional ``cover_image`` path.
        assets: The planned resource set, whose ``mapping`` tracks input-to-output
            resource paths.
        registry: The asset registry for reserving collision-free paths for the
            synthesised cover page.

    Returns:
        A ``CoverPlan`` describing the merged book's cover, or ``None`` if no cover
        is available.

    Raises:
        MergeInputError: If ``options.cover_image`` is set but cannot be read.
    """
    # Rule 1: cover_image option takes precedence
    if options.cover_image is not None:
        try:
            image_data = options.cover_image.read_bytes()
        except OSError as exc:
            raise MergeInputError(
                f'cover image "{options.cover_image}" could not be read: {exc}'
            ) from exc

        suffix = options.cover_image.suffix.lower()
        media_type = COVER_MEDIA_TYPES.get(suffix, "image/jpeg")
        image_path = "cover" + suffix
        page_data = render_cover_page(image_path)

        logger.info("Merge cover taken from the manifest option: %s (%s)", image_path, media_type)

        page_path = registry.reserve("cover.xhtml")
        if page_path != "cover.xhtml":
            logger.debug("Merge cover page reserved a free path: %s", page_path)

        return CoverPlan(
            image_path=image_path,
            image_media_type=media_type,
            extra_files={image_path: image_data},
            page_path=page_path,
            page_data=page_data,
        )

    # Rule 2: survivor's cover_href if it's in the asset mapping
    survivor = books[0]
    if survivor.cover_href is not None:
        mapping_key = (survivor.index, survivor.cover_href)
        if mapping_key in assets.mapping:
            image_path = assets.mapping[mapping_key]
            media_type = survivor.cover_media_type or "image/jpeg"
            page_data = render_cover_page(image_path)

            logger.info(
                "Merge cover carried over from the survivor: %s (%s)", image_path, media_type
            )

            page_path = registry.reserve("cover.xhtml")
            if page_path != "cover.xhtml":
                logger.debug("Merge cover page reserved a free path: %s", page_path)

            return CoverPlan(
                image_path=image_path,
                image_media_type=media_type,
                extra_files={},
                page_path=page_path,
                page_data=page_data,
            )

    # Rule 3: no cover
    logger.debug("Merge produced no cover: no cover_image option and the survivor declares none")
    return None
