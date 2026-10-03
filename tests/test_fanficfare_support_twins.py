"""Every plugin that imports FanFicFare carries the same FanFicFare support module (LIB-D26)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
"""The plugins repository root."""

TWINS = (
    ROOT / "plugins/source/fanficfare_source/fanficfare_source/fff_support.py",
    ROOT / "plugins/catalog/url_story_extractor/url_story_extractor/fff_support.py",
)
"""Every copy of the FanFicFare support module."""

_IMPORTS_FANFICFARE = re.compile(r"^\s*(import fanficfare|from fanficfare)", re.MULTILINE)


def test_every_copy_of_the_fanficfare_support_module_is_identical() -> None:
    """The copies exist and hold the same bytes."""
    missing = [str(p.relative_to(ROOT)) for p in TWINS if not p.is_file()]
    assert missing == [], f"missing copies: {missing}"
    contents = {p: p.read_bytes() for p in TWINS}
    assert len(set(contents.values())) == 1, (
        "these copies differ: "
        + ", ".join(str(p.relative_to(ROOT)) for p in TWINS)
        + " — LIB-D26: copy the file, never edit one copy"
    )


def test_every_plugin_that_imports_fanficfare_carries_the_support_module() -> None:
    """A plugin package importing fanficfare holds fff_support.py beside its modules."""
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in ROOT.glob("plugins/*/*/*/*.py")
        if path.parent.name != "tests"
        and _IMPORTS_FANFICFARE.search(path.read_text(encoding="utf-8"))
        and not (path.parent / "fff_support.py").is_file()
    )
    assert offenders == [], f"import fanficfare without fff_support.py beside them: {offenders}"
