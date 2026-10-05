"""Name the releases the registry index lists: those of plugins that still have a folder.

The index workflow (``.github/workflows/index.yml``) downloads every GitHub release and builds the
registry index from them. A plugin removed from ``plugins/`` must leave the index too (``LIB-D41``
in the core's ``docs/requirements/LIBRARY_FIELDS_REQUIREMENTS.md``), so an installed copy shows as
no longer available, while its
old releases stay on GitHub as history. This script keeps only the releases whose plugin folder
``plugins/<type>/<id>/`` (holding a ``manifest.toml``) still exists.

Usage: ``python scripts/indexed_releases.py <published.json> <indexed.json>`` — reads the
``{tag: published_at}`` map the workflow wrote, writes the kept subset, and reports one line.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
"""The repository root: the folder holding ``plugins/`` and ``registry/``."""


def plugin_ids(root: Path) -> set[str]:
    """Return the id of every plugin folder ``plugins/<type>/<id>/`` holding a ``manifest.toml``.

    Args:
        root: The repository root.

    Returns:
        The folder names, which are the plugin ids.
    """
    return {
        p.name for p in root.glob("plugins/*/*") if p.is_dir() and (p / "manifest.toml").is_file()
    }


def release_plugin_id(tag: str) -> str:
    """Return the plugin id of a release tag ``<id>-<version>``.

    Args:
        tag: The release tag, e.g. ``epub_merge-2.4.0``.

    Returns:
        Everything before the last ``-``.
    """
    return tag.rpartition("-")[0]


def indexed_releases(published: Mapping[str, str], ids: set[str]) -> dict[str, str]:
    """Keep the releases whose plugin id is in *ids*.

    Args:
        published: Publication time per release tag.
        ids: The plugin ids that still have a folder.

    Returns:
        The kept ``{tag: published_at}`` entries, sorted by tag.
    """
    return {tag: published[tag] for tag in sorted(published) if release_plugin_id(tag) in ids}


def main(argv: Sequence[str]) -> int:
    """Filter the published-releases file into the indexed-releases file.

    Args:
        argv: ``[published_json, indexed_json]``.

    Returns:
        0 on success, 2 on a usage error.
    """
    if len(argv) != 2:
        sys.stderr.write("usage: indexed_releases.py <published.json> <indexed.json>\n")
        return 2
    published = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    kept = indexed_releases(published, plugin_ids(ROOT))
    Path(argv[1]).write_text(json.dumps(kept), encoding="utf-8")
    left_out = sorted(set(published) - set(kept))
    sys.stdout.write(
        f"Indexed {len(kept)} of {len(published)} release(s); "
        f"left out: {', '.join(left_out) or 'none'}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
