"""scripts/check.py steps: workflow and dependabot schema checks after the pins."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _load("check")


def test_check_validates_the_workflows_and_dependabot_after_the_pins(tmp_path: Path) -> None:
    """The workflows and dependabot steps sit right after the requirement pins step."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "b.yml").write_text("name: b\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows" / "a.yml").write_text("name: a\n", encoding="utf-8")
    steps = CHECK.steps(tmp_path, "py")
    labels = [s.label for s in steps]
    assert labels[labels.index("requirement pins") + 1 :][:2] == ["workflows", "dependabot"]
    workflow_step = next(s for s in steps if s.label == "workflows")
    assert workflow_step.command == [
        "py",
        "-m",
        "check_jsonschema",
        "--builtin-schema",
        "vendor.github-workflows",
        ".github/workflows/a.yml",
        ".github/workflows/b.yml",
    ]
    dependabot_step = next(s for s in steps if s.label == "dependabot")
    assert dependabot_step.command[-1] == ".github/dependabot.yml"
