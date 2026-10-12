"""The tracked requirement pins: exact == pins agree across manifests and the dev group."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PINS = _load("requirement_pins")


def _repo(tmp_path: Path, manifests: dict[str, list[str]], dev: list[str]) -> Path:
    for plugin_id, requirements in manifests.items():
        folder = tmp_path / "plugins" / "source" / plugin_id
        folder.mkdir(parents=True)
        listed = ", ".join(f'"{r}"' for r in requirements)
        (folder / "manifest.toml").write_text(
            f'id = "{plugin_id}"\nversion = "1.0.0"\nrequirements = [{listed}]\n', encoding="utf-8"
        )
    listed = ", ".join(f'"{r}"' for r in dev)
    (tmp_path / "pyproject.toml").write_text(
        f"[dependency-groups]\ndev = [{listed}]\n", encoding="utf-8"
    )
    return tmp_path


def test_exact_version_reads_a_single_equals_pin() -> None:
    assert PINS.exact_version("fanficfare==4.62.0") == "4.62.0"


def test_ranges_wildcards_and_compound_specifiers_are_not_pins() -> None:
    for raw in ("pkg>=1.0", "pkg==1.*", "pkg==1.0,<2", "pkg"):
        assert PINS.exact_version(raw) is None, raw


def test_extras_and_markers_keep_the_pin() -> None:
    assert PINS.exact_version('pkg[extra]==1.0; python_version >= "3.11"') == "1.0"


def test_tracked_pins_group_manifests_by_canonical_name(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, {"a": ["FanFicFare==4.62.0"], "b": ["fanficfare==4.62.0"]}, ["fanficfare==4.62.0"]
    )
    pins = PINS.tracked_pins(root)
    assert list(pins) == ["fanficfare"]
    assert pins["fanficfare"].version == "4.62.0"
    assert [u.plugin_id for u in pins["fanficfare"].uses] == ["a", "b"]


def test_two_versions_of_one_package_raise(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"a": ["fanficfare==4.62.0"], "b": ["fanficfare==4.63.0"]}, [])
    with pytest.raises(PINS.PinError, match=r"^fanficfare: a pins 4\.62\.0, b pins 4\.63\.0$"):
        PINS.tracked_pins(root)


def test_a_tracked_pin_missing_from_the_dev_group_is_a_problem(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, {"a": ["fanficfare==4.62.0"], "b": ["fanficfare==4.62.0"]}, ["pytest>=8"]
    )
    assert PINS.problems(root) == [
        "fanficfare: pinned to 4.62.0 by a, b but missing from [dependency-groups] dev"
    ]


def test_a_dev_group_that_disagrees_is_a_problem(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"a": ["fanficfare==4.62.0"]}, ["fanficfare>=4.62.0"])
    assert PINS.problems(root) == [
        "fanficfare: pinned to 4.62.0 by a but [dependency-groups] dev has fanficfare>=4.62.0"
    ]


def test_agreeing_pins_have_no_problem(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"a": ["pymupdf==1.28.2", "charset-normalizer>=3.3"]},
        ["pymupdf==1.28.2", "charset-normalizer>=3.3"],
    )
    assert PINS.problems(root) == []


def test_two_versions_are_reported_as_a_problem(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"a": ["fanficfare==4.62.0"], "b": ["fanficfare==4.63.0"]}, [])
    assert PINS.problems(root) == ["fanficfare: a pins 4.62.0, b pins 4.63.0"]


def test_check_lists_the_pins_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        tmp_path, {"a": ["FanFicFare==4.62.0"], "b": ["fanficfare==4.62.0"]}, ["fanficfare==4.62.0"]
    )
    assert PINS.main(["check"], root=root) == 0
    assert capsys.readouterr().out == "fanficfare==4.62.0: a, b\n1 tracked pin(s) agree\n"


def test_check_lists_the_problems_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"a": ["fanficfare==4.62.0"]}, ["fanficfare>=4.62.0"])
    assert PINS.main(["check"], root=root) == 1
    assert capsys.readouterr().out.endswith("1 problem(s) with the tracked pins\n")


def test_the_repository_itself_agrees() -> None:
    assert PINS.problems(ROOT) == []


def test_check_runs_the_pins_step_after_types() -> None:
    check = _load("check")
    steps = check.steps(ROOT, "python")
    labels = [s.label for s in steps]
    i = labels.index("types")
    assert labels[i + 1] == "requirement pins"
    assert steps[i + 1].command == ["python", "scripts/requirement_pins.py", "check"]
