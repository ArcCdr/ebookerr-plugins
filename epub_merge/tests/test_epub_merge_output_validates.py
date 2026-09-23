"""Regression net: a merged EPUB must validate with zero errors (``§C.8``)."""

from __future__ import annotations

import shutil
from pathlib import Path

from ebookerr_sdk.validate import validate_epub
from epub_merge.merge.merge import merge_epubs

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tending_bar.epub"
_FIXTURE2 = Path(__file__).resolve().parent / "fixtures" / "AIF35.epub"


def _errors(path: Path) -> list[str]:
    """Every error-severity finding, rendered as 'CODE at LOCATION: MESSAGE'."""
    report = validate_epub(path)
    return [
        f"{f.code} at {f.location}: {f.message}" for f in report.findings if f.severity == "error"
    ]


class TestMerge:
    """Tests for merge_epubs()."""

    def test_merge_output_has_no_errors(self, tmp_path: Path) -> None:
        """Merged EPUBs validate with zero errors."""
        m = tmp_path / "m.epub"
        s = tmp_path / "s.epub"
        shutil.copy(_FIXTURE, m)
        shutil.copy(_FIXTURE2, s)
        # merge_epubs modifies target in place and returns MergeOutcome
        assert merge_epubs(m, [s]).chapter_count == 7
        assert _errors(m) == []

    def test_merge_output_is_epub3_with_a_nav_document(self, tmp_path: Path) -> None:
        """Merged output is EPUB3 with nav.xhtml outside the spine (EBK-OPF-15 exemption)."""
        m = tmp_path / "m.epub"
        s = tmp_path / "s.epub"
        shutil.copy(_FIXTURE, m)
        shutil.copy(_FIXTURE2, s)
        merge_epubs(m, [s])
        report = validate_epub(m)
        codes = {f.code for f in report.findings}
        assert "EBK-OPF-15" not in codes
        assert "EBK-OPF-16" not in codes
