"""The plugins repository's own content rules (moved from the core's gates in ebookerr 2.20.1)."""

from __future__ import annotations

import sys
import tomllib
from ast import Import, ImportFrom, parse, walk
from pathlib import Path
from typing import Any

from ebookerr_sdk.pack.imports import SDK_PROVIDED_IMPORTS, requirement_import_names

ROOT = Path(__file__).resolve().parents[1]
TEST_ONLY = frozenset({"pytest", "_pytest", "responses", "reportlab"})
FORBIDDEN = frozenset({"src", "tests"})


def plugin_folders(root: Path) -> list[Path]:
    """Folders ``plugins/<type>/<id>/`` holding a ``manifest.toml``, sorted."""
    return sorted(
        p for p in root.glob("plugins/*/*") if p.is_dir() and (p / "manifest.toml").is_file()
    )


def manifest(folder: Path) -> dict[str, Any]:
    """The folder's parsed ``manifest.toml``."""
    return tomllib.loads((folder / "manifest.toml").read_text(encoding="utf-8"))


def import_roots(path: Path) -> list[tuple[int, str]]:
    """``(line, top-level module)`` for every absolute import in *path*."""
    tree = parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in walk(tree):
        if isinstance(node, Import):
            found.extend((node.lineno, a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


def plugin_test_import_offences(root: Path) -> list[str]:
    """Forbidden imports in plugin tests and root conftest, as ``<rel>:<line> imports <module>``."""
    offences: list[str] = []

    for folder in plugin_folders(root):
        allowed = (
            frozenset(sys.stdlib_module_names)
            | SDK_PROVIDED_IMPORTS
            | TEST_ONLY
            | {p.name for p in folder.iterdir() if p.is_dir()}
            | {p.stem for p in folder.glob("*.py")}
            | {p.stem for p in (folder / "tests").glob("*.py")}
            | requirement_import_names(manifest(folder).get("requirements", []))
        )

        test_dir = folder / "tests"
        if test_dir.is_dir():
            for test_file in test_dir.glob("*.py"):
                if test_file.name == "__init__.py":
                    continue
                for line, module in import_roots(test_file):
                    if module in FORBIDDEN or module not in allowed:
                        offences.append(f"{test_file.relative_to(root)}:{line} imports {module}")

    root_conftest = root / "conftest.py"
    if root_conftest.is_file():
        allowed_root = frozenset(sys.stdlib_module_names) | {"pytest", "_pytest"}
        for line, module in import_roots(root_conftest):
            if module not in allowed_root:
                offences.append(f"{root_conftest.relative_to(root)}:{line} imports {module}")

    return sorted(offences)


def test_the_registry_lists_every_plugin_folder_as_official() -> None:
    """Registry lists every plugin folder as official."""
    registry_path = ROOT / "registry" / "registry.toml"
    data = tomllib.loads(registry_path.read_text(encoding="utf-8"))

    # Check header values
    assert data.get("id") == "ebookerr"
    assert data.get("name") == "ebookerr"
    assert data.get("maintainer") == "ArcCdr"
    assert data.get("homepage") == "https://github.com/ArcCdr/ebookerr-plugins"
    assert data.get("base_url") == "https://github.com/ArcCdr/ebookerr-plugins/releases/download"

    # Check that registry plugins match folder names
    folder_names = sorted(f.name for f in plugin_folders(ROOT))
    registry_names = sorted(data.get("plugins", {}).keys())
    assert registry_names == folder_names

    # Check that every plugin has official = true
    for plugin_id, plugin_data in data.get("plugins", {}).items():
        assert plugin_data == {"official": True}, (
            f"Plugin {plugin_id} does not have official = true"
        )


def test_every_plugin_folder_sits_in_its_type_folder() -> None:
    """Every plugin folder sits in its type folder."""
    for folder in plugin_folders(ROOT):
        m = manifest(folder)
        assert folder.parent.name == m["type"], (
            f"Plugin {folder.name} in {folder.parent.name}/ but manifest says type={m['type']}"
        )


def test_plugin_test_basenames_are_unique() -> None:
    """Plugin test basenames are unique across all plugins."""
    basenames: dict[str, Path] = {}
    for folder in plugin_folders(ROOT):
        test_dir = folder / "tests"
        if test_dir.is_dir():
            for test_file in test_dir.glob("*.py"):
                basename = test_file.name
                if basename in basenames:
                    raise AssertionError(
                        f"Test basename {basename} appears in both {basenames[basename]} "
                        f"and {test_file}"
                    )
                basenames[basename] = test_file


def test_no_plugin_tests_folder_is_a_package() -> None:
    """No plugin tests folder has an __init__.py."""
    init_files = list(ROOT.glob("plugins/*/*/tests/__init__.py"))
    assert init_files == [], f"Found __init__.py in tests folders: {init_files}"


def test_plugin_tests_import_only_what_a_plugin_test_may() -> None:
    """Plugin tests import only what a plugin test may."""
    offences = plugin_test_import_offences(ROOT)
    assert offences == [], f"Forbidden imports found: {offences}"


def test_the_import_rule_flags_a_core_import(tmp_path: Path) -> None:
    """The import rule flags core imports in plugin tests."""
    # Create a test plugin structure
    plugin_dir = tmp_path / "plugins" / "book" / "p"
    test_dir = plugin_dir / "tests"
    test_dir.mkdir(parents=True)

    # Create manifest
    (plugin_dir / "manifest.toml").write_text('id = "p"\ntype = "book"\n')

    # Create test with core import
    (test_dir / "test_p.py").write_text("from src.services.x import y\n")

    # Check that offence is detected
    offences = plugin_test_import_offences(tmp_path)
    assert offences == ["plugins/book/p/tests/test_p.py:1 imports src"]


def test_the_root_conftest_rule_flags_the_sdk(tmp_path: Path) -> None:
    """The root conftest rule flags SDK imports."""
    # Create conftest.py with SDK import
    (tmp_path / "conftest.py").write_text(
        "import pytest\nfrom ebookerr_sdk.testing import FakeContext\n"
    )

    # Check that offence is detected
    offences = plugin_test_import_offences(tmp_path)
    assert offences == ["conftest.py:2 imports ebookerr_sdk"]
