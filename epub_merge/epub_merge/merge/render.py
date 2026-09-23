"""Serialize a merge plan into an in-memory EPUB archive, ready to save.

This module is the thin seam between a :class:`MergePlan` (describing what
the output EPUB will contain) and the shared :mod:`ebookerr_sdk.epub.builder`
and :class:`EpubArchive` machinery (handling how it is serialized). It performs
no policy decisions, only assembly: it collects the members in the prescribed
order, builds fixed documents from the plan's metadata, and hands them to the
archive constructor.
"""

from __future__ import annotations

import logging

from ebookerr_sdk.epub import EpubArchive
from ebookerr_sdk.epub.builder import build_container_xml, build_nav, build_ncx, build_opf

from epub_merge.merge.model import NAV_HREF, NCX_HREF, OPF_DIR, OPF_PATH
from epub_merge.merge.plan import MergePlan

logger = logging.getLogger(__name__)


def render_plan(plan: MergePlan) -> EpubArchive:
    """Serialize a merge plan into an in-memory EPUB archive, ready to save.

    Assembles the archive in the standard member order:
    1. ``mimetype`` → ``b"application/epub+zip"``
    2. ``META-INF/container.xml`` → output of :func:`build_container_xml`
    3. ``OEBPS/content.opf`` → output of :func:`build_opf`
    4. ``OEBPS/toc.ncx`` → output of :func:`build_ncx`
    5. ``OEBPS/nav.xhtml`` (EPUB3 only) → output of :func:`build_nav`
    6. Every entry of ``plan.files``, sorted by path, as ``OEBPS/{path}``

    Args:
        plan: The merge plan to serialize.

    Returns:
        An :class:`EpubArchive` holding all members in write order.
    """
    members: dict[str, bytes] = {}
    order: list[str] = []

    # 1. mimetype (first, as per EPUB spec)
    order.append("mimetype")
    members["mimetype"] = b"application/epub+zip"

    # 2. META-INF/container.xml
    container_name = "META-INF/container.xml"
    order.append(container_name)
    members[container_name] = build_container_xml(OPF_PATH)

    # 3. OEBPS/content.opf
    opf_name = OPF_PATH
    order.append(opf_name)
    members[opf_name] = build_opf(plan.package)

    # 4. OEBPS/toc.ncx
    ncx_name = f"{OPF_DIR}/{NCX_HREF}"
    order.append(ncx_name)
    members[ncx_name] = build_ncx(title=plan.title, identifier=plan.identifier, entries=plan.toc)

    # 5. OEBPS/nav.xhtml (only for EPUB3)
    if plan.version.startswith("3"):
        nav_name = f"{OPF_DIR}/{NAV_HREF}"
        order.append(nav_name)
        members[nav_name] = build_nav(
            title=plan.title,
            entries=plan.toc,
            landmarks=plan.landmarks,
            language=plan.package.metadata.language,
        )

    # 6. Every file from plan.files, sorted by path
    for path in sorted(plan.files):
        file_name = f"{OPF_DIR}/{path}"
        order.append(file_name)
        members[file_name] = plan.files[path]

    logger.debug("Merge rendered %d archive member(s), version=%s", len(order), plan.version)

    return EpubArchive(members, order, source=None)
