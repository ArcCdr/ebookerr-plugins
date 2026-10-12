"""Exact requirement pins: the tracked dependencies of the first-party plugins.

A manifest requirement whose specifier is a single ``==`` pin is *tracked*: every manifest that
names the package pins the same version, and the repository's ``[dependency-groups] dev`` holds
the same ``name==version`` entry, so CI tests exactly the version every user installs, and a
dependency release moves all of them together. ``python scripts/requirement_pins.py check`` checks
the rule and lists the tracked pins (``scripts/check.py`` runs it).

Usage: ``python scripts/requirement_pins.py check`` (from any directory).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
"""The repository root: the folder holding ``plugins/`` and ``pyproject.toml``."""


class PinError(Exception):
    """Two manifests pin one package to different versions."""


@dataclass(frozen=True)
class PinUse:
    """One manifest's requirement entry for a tracked package.

    Attributes:
        plugin_id: The plugin folder's name (its id).
        manifest: Path of that plugin's ``manifest.toml``.
        raw: The requirement exactly as written, e.g. ``"fanficfare==4.62.0"``.
    """

    plugin_id: str
    manifest: Path
    raw: str


@dataclass(frozen=True)
class TrackedPin:
    """A package pinned with ``==`` by at least one plugin.

    Attributes:
        name: The canonical package name, e.g. ``"python-docx"``.
        version: The pinned version, e.g. ``"1.2.0"``.
        uses: Every manifest entry pinning it, sorted by plugin id.
    """

    name: str
    version: str
    uses: tuple[PinUse, ...]


def exact_version(requirement: str) -> str | None:
    """Return the version of *requirement* when it is one ``==`` pin without a wildcard.

    Args:
        requirement: A PEP 508 requirement string.

    Returns:
        The pinned version, or None for a range, a wildcard or no specifier.
    """
    specifiers = list(Requirement(requirement).specifier)
    if len(specifiers) != 1:
        return None
    spec = specifiers[0]
    if spec.operator != "==" or spec.version.endswith(".*"):
        return None
    return spec.version


def plugin_folders(root: Path) -> list[Path]:
    """Return the ``plugins/<type>/<id>/`` folders holding a ``manifest.toml``, sorted.

    Args:
        root: The repository root.

    Returns:
        The plugin folders, sorted by path (type, then id).
    """
    return sorted(
        p for p in root.glob("plugins/*/*") if p.is_dir() and (p / "manifest.toml").is_file()
    )


def _manifest_requirements(folder: Path) -> list[str]:
    """Return the ``requirements`` of the plugin in *folder* (empty when it declares none)."""
    data = tomllib.loads((folder / "manifest.toml").read_text(encoding="utf-8"))
    return [str(raw) for raw in data.get("requirements", [])]


def tracked_pins(root: Path) -> dict[str, TrackedPin]:
    """Return every tracked package of the plugins under *root*, keyed by canonical name.

    Args:
        root: The repository root.

    Returns:
        The tracked pins in name order.

    Raises:
        PinError: Two manifests pin one package to different versions.
    """
    versions: dict[str, str] = {}
    uses: dict[str, list[PinUse]] = {}
    for folder in plugin_folders(root):
        for raw in _manifest_requirements(folder):
            version = exact_version(raw)
            if version is None:
                continue
            name = canonicalize_name(Requirement(raw).name)
            use = PinUse(plugin_id=folder.name, manifest=folder / "manifest.toml", raw=raw)
            if name in versions and versions[name] != version:
                first = uses[name][0]
                msg = (
                    f"{name}: {first.plugin_id} pins {versions[name]}, "
                    f"{use.plugin_id} pins {version}"
                )
                raise PinError(msg)
            versions[name] = version
            uses.setdefault(name, []).append(use)
    return {
        name: TrackedPin(name, versions[name], tuple(sorted(uses[name], key=_by_plugin)))
        for name in sorted(versions)
    }


def _by_plugin(use: PinUse) -> str:
    """Sort key of a pin use: its plugin id."""
    return use.plugin_id


def dev_group_entries(pyproject_text: str) -> dict[str, str]:
    """Return canonical name → requirement string for every entry of ``[dependency-groups] dev``.

    Args:
        pyproject_text: The text of a ``pyproject.toml``.

    Returns:
        The entries (an ``include-group`` table is skipped).
    """
    data = tomllib.loads(pyproject_text)
    dev = data.get("dependency-groups", {}).get("dev", [])
    return {canonicalize_name(Requirement(raw).name): raw for raw in dev if isinstance(raw, str)}


def problems(root: Path) -> list[str]:
    """Return every breach of the tracked-pin rule under *root*, one message each, sorted.

    Args:
        root: The repository root.

    Returns:
        The problems; empty when every tracked pin agrees.
    """
    try:
        pins = tracked_pins(root)
    except PinError as exc:
        return [str(exc)]
    dev = dev_group_entries((root / "pyproject.toml").read_text(encoding="utf-8"))
    found: list[str] = []
    for name, pin in pins.items():
        ids = ", ".join(use.plugin_id for use in pin.uses)
        entry = dev.get(name)
        if entry is None:
            found.append(
                f"{name}: pinned to {pin.version} by {ids} but missing from [dependency-groups] dev"
            )
        elif exact_version(entry) != pin.version:
            found.append(
                f"{name}: pinned to {pin.version} by {ids} but [dependency-groups] dev has {entry}"
            )
    return sorted(found)


def _check(root: Path) -> int:
    """Print the tracked pins, or the problems; return 0 when there are none, else 1."""
    found = problems(root)
    if found:
        for line in found:
            sys.stdout.write(f"{line}\n")
        sys.stdout.write(f"{len(found)} problem(s) with the tracked pins\n")
        return 1
    pins = tracked_pins(root)
    for name, pin in pins.items():
        ids = ", ".join(use.plugin_id for use in pin.uses)
        sys.stdout.write(f"{name}=={pin.version}: {ids}\n")
    sys.stdout.write(f"{len(pins)} tracked pin(s) agree\n")
    return 0


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT) -> int:
    """Run a subcommand of this script.

    Args:
        argv: The arguments after the program name (``sys.argv[1:]`` when None).
        root: The repository root (a test passes a throwaway one).

    Returns:
        The subcommand's exit code.
    """
    parser = argparse.ArgumentParser(
        prog="python scripts/requirement_pins.py",
        description="The tracked (exact ==) requirement pins of the first-party plugins.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Check that every tracked pin agrees, and list them.")
    args = parser.parse_args(argv)
    return _check(root) if args.command == "check" else 2


if __name__ == "__main__":
    raise SystemExit(main())
