"""Exact requirement pins: the tracked dependencies of the first-party plugins.

A manifest requirement whose specifier is a single ``==`` pin is *tracked*: every manifest that
names the package pins the same version, and the repository's ``[dependency-groups] dev`` holds
the same ``name==version`` entry, so CI tests exactly the version every user installs, and a
dependency release moves all of them together. ``python scripts/requirement_pins.py check`` checks
the rule and lists the tracked pins (``scripts/check.py`` runs it).

``python scripts/requirement_pins.py plan [--cooldown-days N] [--package NAME]`` prints, as JSON,
the updates due: for each tracked package the newest PyPI release that is final, not yanked, at
least *N* days old (default 3) and installable on this Python.
``python scripts/requirement_pins.py apply NAME VERSION --display DISPLAY`` rewrites the repository
for one update: every manifest pinning the package, the plugin version (a patch bump when the
current version is already tagged), each plugin's CHANGELOG and the ``[dependency-groups] dev``
pin. The ``dependency-watch`` workflow runs both; ``dependency-release`` merges and tags what CI
passed.

Usage: ``python scripts/requirement_pins.py check|plan|apply …`` (from any directory).
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import requests
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

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

DEFAULT_COOLDOWN_DAYS = 3
"""Days a release must have been public before the bot takes it."""

HTTP_TIMEOUT: tuple[float, float] = (5, 30)
"""Connect and read timeouts of a PyPI request, in seconds."""

_VERSION_LINE = re.compile(r'^version = "(?P<version>[^"]+)"$', re.MULTILINE)
"""A manifest's own ``version = "X.Y.Z"`` line."""


@dataclass(frozen=True)
class Update:
    """One tracked package with a newer eligible release.

    Attributes:
        name: Canonical package name, e.g. ``"fanficfare"``.
        display: PyPI's ``info.name``, e.g. ``"FanFicFare"``.
        current: The pinned version.
        version: The version to move to.
        plugin_ids: The plugins that pin it, sorted.
    """

    name: str
    display: str
    current: str
    version: str
    plugin_ids: tuple[str, ...]

    @property
    def branch(self) -> str:
        """``"deps/<name>"`` — the one branch the bot keeps for this package."""
        return f"deps/{self.name}"

    @property
    def title(self) -> str:
        """``"deps: <display> <version>"`` — the commit and pull-request title."""
        return f"deps: {self.display} {self.version}"

    def as_dict(self) -> dict[str, object]:
        """Return the update as the JSON object ``plan`` prints (the workflow reads it)."""
        ids = ", ".join(self.plugin_ids)
        body = (
            f"Updates {self.display} from {self.current} to {self.version} in {ids}.\n\n"
            f"Release: https://pypi.org/project/{self.name}/{self.version}/\n\n"
            "CI merges this pull request and releases the plugins when the checks pass."
        )
        return {
            "name": self.name,
            "display": self.display,
            "current": self.current,
            "version": self.version,
            "plugins": list(self.plugin_ids),
            "branch": self.branch,
            "title": self.title,
            "body": body,
        }


def _files_allow(files: Sequence[Mapping[str, Any]], *, cutoff: datetime, python: str) -> bool:
    """Whether a release's *files* allow it: present, none yanked, old enough, installable."""
    if not files or any(f.get("yanked") for f in files):
        return False
    uploaded = min(datetime.fromisoformat(str(f["upload_time_iso_8601"])) for f in files)
    if uploaded > cutoff:
        return False
    return any(
        not f.get("requires_python") or SpecifierSet(str(f["requires_python"])).contains(python)
        for f in files
    )


def eligible_version(
    release_json: Mapping[str, Any],
    *,
    current: str,
    now: datetime,
    cooldown_days: int,
    python_version: str,
) -> str | None:
    """Return the newest release in PyPI's JSON newer than *current* that the rules allow.

    A release is eligible when it is final (not a pre-release or a dev release), has at least
    one file, none of its files is yanked, its earliest upload is at least *cooldown_days* old,
    and at least one file installs on *python_version*.

    Args:
        release_json: PyPI's JSON for one project (``releases`` maps a version to its files).
        current: The pinned version.
        now: The current time (timezone-aware).
        cooldown_days: Days a release must have been public.
        python_version: The interpreter version the files must allow, e.g. ``"3.14.0"``.

    Returns:
        The version to move to, or None when no release qualifies.
    """
    cutoff = now - timedelta(days=cooldown_days)
    floor = Version(current)
    best: Version | None = None
    for text, files in release_json.get("releases", {}).items():
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease or version <= floor:
            continue
        if not _files_allow(files, cutoff=cutoff, python=python_version):
            continue
        if best is None or version > best:
            best = version
    return None if best is None else str(best)


def plan(
    root: Path,
    *,
    now: datetime,
    cooldown_days: int,
    fetch: Callable[[str], Mapping[str, Any]],
    python_version: str,
    only: str | None = None,
) -> list[Update]:
    """Return the updates due for the tracked pins under *root*, in name order.

    Args:
        root: The repository root.
        now: The current time (timezone-aware).
        cooldown_days: Days a release must have been public.
        fetch: Returns PyPI's JSON for a canonical package name.
        python_version: The interpreter version the files must allow.
        only: Keep only this package (any spelling of its name); None keeps all.

    Returns:
        One update per tracked package with an eligible newer release.
    """
    updates: list[Update] = []
    for name, pin in tracked_pins(root).items():
        if only is not None and canonicalize_name(only) != name:
            continue
        release_json = fetch(name)
        version = eligible_version(
            release_json,
            current=pin.version,
            now=now,
            cooldown_days=cooldown_days,
            python_version=python_version,
        )
        if version is None:
            continue
        display = str(release_json.get("info", {}).get("name") or name)
        plugin_ids = tuple(use.plugin_id for use in pin.uses)
        updates.append(Update(name, display, pin.version, version, plugin_ids))
    return updates


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
    sentence = changelog_sentence(display, version)
    changed = [_apply_to_plugin(use, pin.version, version, sentence, tagged) for use in pin.uses]
    pyproject.write_text(new_pyproject, encoding="utf-8")
    return changed


def _plan_command(args: argparse.Namespace, root: Path) -> int:
    """Print the updates due as JSON; return 0."""
    updates = plan(
        root,
        now=datetime.now(UTC),
        cooldown_days=args.cooldown_days,
        fetch=fetch_release_json,
        python_version=platform.python_version(),
        only=args.package or None,
    )
    sys.stdout.write(json.dumps([update.as_dict() for update in updates], indent=2) + "\n")
    return 0


def _apply_command(args: argparse.Namespace, root: Path) -> int:
    """Apply one update and print the plugins it changed; return 0."""
    for line in apply(
        root, args.name, args.version, display=args.display, tagged=partial(is_tagged, root)
    ):
        sys.stdout.write(f"{line}\n")
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
    plan_parser = commands.add_parser("plan", help="Print, as JSON, the updates due.")
    plan_parser.add_argument("--cooldown-days", type=int, default=DEFAULT_COOLDOWN_DAYS)
    plan_parser.add_argument("--package", default="")
    apply_parser = commands.add_parser("apply", help="Rewrite the repository for one update.")
    apply_parser.add_argument("name")
    apply_parser.add_argument("version")
    apply_parser.add_argument("--display", required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        return _plan_command(args, root)
    if args.command == "apply":
        return _apply_command(args, root)
    return _check(root)


if __name__ == "__main__":
    raise SystemExit(main())
