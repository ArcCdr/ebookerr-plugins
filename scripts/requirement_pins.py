"""Exact requirement pins: the tracked dependencies of the first-party plugins.

A manifest requirement whose specifier is a single ``==`` pin is *tracked*: every manifest that
names the package pins the same version, and the repository's ``[dependency-groups] dev`` holds
the same ``name==version`` entry, so CI tests exactly the version every user installs.

Dependabot moves the dev-group pin and ``uv.lock`` on its own pull request.
``python scripts/requirement_pins.py sync`` (run by ``dependency-sync.yml``) moves the manifests
to it: the pin, the plugin version (a patch bump when the current version is already tagged) and
each plugin's CHANGELOG. ``python scripts/requirement_pins.py apply NAME VERSION --display
DISPLAY`` moves everything by hand (the dev pin too) and re-locks with ``uv lock``.
``python scripts/requirement_pins.py check`` is the rule ``scripts/check.py`` runs: every pin
agrees, and lists the tracked pins.

Usage: ``python scripts/requirement_pins.py check|sync|apply …`` (from any directory).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import requests
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


PYPI_URL = "https://pypi.org/pypi/{name}/json"
"""PyPI's JSON API for one project; ``{name}`` is the canonical package name."""

HTTP_TIMEOUT: tuple[float, float] = (5, 30)
"""Connect and read timeouts of a PyPI request, in seconds."""

_VERSION_LINE = re.compile(r'^version = "(?P<version>[^"]+)"$', re.MULTILINE)
"""A manifest's own ``version = "X.Y.Z"`` line."""


def fetch_release_json(name: str) -> dict[str, Any]:
    """Return PyPI's JSON for *name*.

    Args:
        name: The canonical package name.

    Returns:
        The decoded JSON document.

    Raises:
        requests.HTTPError: PyPI answered with an error status.
    """
    response = requests.get(
        PYPI_URL.format(name=name), timeout=HTTP_TIMEOUT, headers={"Accept": "application/json"}
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def bump_patch(version: str) -> str:
    """Return *version* with its patch number raised by one (``"2.0.1"`` → ``"2.0.2"``).

    Args:
        version: An ``X.Y.Z`` version.

    Returns:
        The next patch version.

    Raises:
        ValueError: *version* is not ``X.Y.Z`` with three numbers.
    """
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"{version!r} is not an X.Y.Z version")
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


def changelog_sentence(display: str, version: str) -> str:
    """Return a dependency release's CHANGELOG sentence: ``"Updated to <display> <version>."``."""
    return f"Updated to {display} {version}."


def is_tagged(root: Path, tag: str) -> bool:
    """Return whether ``git tag --list <tag>`` names *tag* in *root* (CI checks out every tag).

    Args:
        root: The repository root.
        tag: The tag, e.g. ``"fanficfare_source-2.0.2"``.

    Returns:
        True when the tag exists.
    """
    result = subprocess.run(
        ["git", "tag", "--list", tag], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() == tag


def _prepend_entry(changelog: Path, version: str, sentence: str) -> None:
    """Put a new ``== <version> ==`` entry holding *sentence* on top of *changelog*."""
    old = changelog.read_text(encoding="utf-8")
    changelog.write_text(f"== {version} ==\n{sentence}\n\n{old}", encoding="utf-8")


def _append_to_top_entry(changelog: Path, sentence: str) -> None:
    """Add *sentence* to the end of the top entry's first paragraph, unless it is there already."""
    lines = changelog.read_text(encoding="utf-8").split("\n")
    end = 1
    while end < len(lines) and lines[end].strip():
        end += 1
    if any(sentence in line for line in lines[1:end]):
        return
    if end == 1:
        lines.insert(1, sentence)
    else:
        lines[end - 1] = f"{lines[end - 1]} {sentence}"
    changelog.write_text("\n".join(lines), encoding="utf-8")


def _apply_to_plugin(
    use: PinUse, old: str, version: str, sentence: str, tagged: Callable[[str], bool]
) -> str:
    """Move one plugin's pin from *old* to *version*; return ``"<plugin id> <plugin version>"``."""
    text = use.manifest.read_text(encoding="utf-8")
    new_raw = use.raw.replace(f"=={old}", f"=={version}", 1)
    text = text.replace(f'"{use.raw}"', f'"{new_raw}"', 1)
    match = _VERSION_LINE.search(text)
    if match is None:
        raise ValueError(f"{use.manifest} has no version line")
    current = match.group("version")
    changelog = use.manifest.parent / "CHANGELOG.txt"
    if tagged(f"{use.plugin_id}-{current}"):
        plugin_version = bump_patch(current)
        text = f'{text[: match.start()]}version = "{plugin_version}"{text[match.end() :]}'
        _prepend_entry(changelog, plugin_version, sentence)
    else:
        plugin_version = current
        _append_to_top_entry(changelog, sentence)
    use.manifest.write_text(text, encoding="utf-8")
    return f"{use.plugin_id} {plugin_version}"


def _dev_pin_rewrite(pyproject: Path, pin: TrackedPin, version: str) -> str:
    """Return *pyproject*'s text with the dev-group pin of *pin* moved to *version*.

    Raises:
        ValueError: The dev group has no single exact entry for the package at its old version.
    """
    text = pyproject.read_text(encoding="utf-8")
    entry = dev_group_entries(text).get(pin.name)
    if entry is None or exact_version(entry) != pin.version or text.count(f'"{entry}"') != 1:
        msg = f"{pin.name}: [dependency-groups] dev has no single {pin.name}=={pin.version} entry"
        raise ValueError(msg)
    new_entry = entry.replace(f"=={pin.version}", f"=={version}", 1)
    return text.replace(f'"{entry}"', f'"{new_entry}"', 1)


def _move_manifests(
    pin: TrackedPin, version: str, display: str, tagged: Callable[[str], bool]
) -> list[str]:
    """Move every manifest using *pin* to *version*; return ``"<plugin id> <plugin version>"``."""
    sentence = changelog_sentence(display, version)
    return [_apply_to_plugin(use, pin.version, version, sentence, tagged) for use in pin.uses]


def apply(
    root: Path, name: str, version: str, *, display: str, tagged: Callable[[str], bool]
) -> list[str]:
    """Rewrite the repository for one update and return the plugins it changed.

    Every manifest pinning *name* moves to *version*; a plugin whose current version is already
    tagged gets the next patch version and a new CHANGELOG entry, one still pending gets the
    sentence added to its top entry; the dev-group pin moves too. Applying the same update twice
    changes nothing the second time.

    Args:
        root: The repository root.
        name: The package (any spelling of its name).
        version: The version to move to.
        display: PyPI's spelling of the package name, for the CHANGELOG sentence.
        tagged: Whether a ``<plugin id>-<version>`` tag exists.

    Returns:
        ``"<plugin id> <plugin version>"`` per plugin changed; empty when nothing changed.

    Raises:
        ValueError: The package is not tracked, or the dev group has no matching exact entry.
    """
    pin = tracked_pins(root).get(canonicalize_name(name))
    if pin is None:
        raise ValueError(f"{name} is not a tracked pin")
    if pin.version == version:
        return []
    pyproject = root / "pyproject.toml"
    new_pyproject = _dev_pin_rewrite(pyproject, pin, version)
    changed = _move_manifests(pin, version, display, tagged)
    pyproject.write_text(new_pyproject, encoding="utf-8")
    return changed


def sync(root: Path, *, display: Callable[[str], str], tagged: Callable[[str], bool]) -> list[str]:
    """Move every manifest to the version the dev group pins, and return the plugins changed.

    Dependabot moves the dev-group pin (and ``uv.lock``) on its own pull request; this follows it.
    The dev group is not written.

    Args:
        root: The repository root.
        display: PyPI's spelling of a package name, for the CHANGELOG sentence.
        tagged: Whether a ``<plugin id>-<version>`` tag exists.

    Returns:
        ``"<plugin id> <plugin version>"`` per plugin changed; empty when every pin agrees.

    Raises:
        ValueError: A tracked package has no exact entry in the dev group.
    """
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    entries = dev_group_entries(text)
    changed: list[str] = []
    for name, pin in sorted(tracked_pins(root).items()):
        entry = entries.get(name)
        dev_version = exact_version(entry) if entry is not None else None
        if dev_version is None:
            msg = f"{name}: [dependency-groups] dev has no single {name}=={pin.version} entry"
            raise ValueError(msg)
        if dev_version != pin.version:
            changed += _move_manifests(pin, dev_version, display(name), tagged)
    return changed


def relock(
    root: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Run ``uv lock`` in *root* and return its exit code."""
    return run(["uv", "lock"], cwd=root, check=False).returncode


def _sync_command(root: Path) -> int:
    """Move the manifests to the dev group's pins and print the plugins changed; return 0."""

    def display(name: str) -> str:
        """Return PyPI's spelling of *name*."""
        return str(fetch_release_json(name).get("info", {}).get("name") or name)

    changed = sync(root, display=display, tagged=partial(is_tagged, root))
    for line in changed:
        sys.stdout.write(f"{line}\n")
    if not changed:
        sys.stdout.write("Every tracked pin already matches the dev group\n")
    return 0


def _apply_command(args: argparse.Namespace, root: Path) -> int:
    """Apply one update, print the plugins it changed and re-lock; return 1 if the lock fails."""
    for line in apply(
        root, args.name, args.version, display=args.display, tagged=partial(is_tagged, root)
    ):
        sys.stdout.write(f"{line}\n")
    code = relock(root, run=subprocess.run)
    if code != 0:
        sys.stderr.write(f"uv lock failed (exit {code})\n")
        return 1
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
    commands.add_parser("sync", help="Move the manifests to the dev group's pins.")
    apply_parser = commands.add_parser("apply", help="Rewrite the repository for one update.")
    apply_parser.add_argument("name")
    apply_parser.add_argument("version")
    apply_parser.add_argument("--display", required=True)
    args = parser.parse_args(argv)
    if args.command == "sync":
        return _sync_command(root)
    if args.command == "apply":
        return _apply_command(args, root)
    return _check(root)


if __name__ == "__main__":
    raise SystemExit(main())
