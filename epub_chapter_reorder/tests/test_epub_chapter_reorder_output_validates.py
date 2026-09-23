"""Regression net: EPUB output from reorder_epub() validates with zero errors."""

from __future__ import annotations

import shutil
from pathlib import Path

from ebookerr_sdk.validate import validate_epub
from epub_chapter_reorder.reorder_step import reorder_epub


def _errors(path: Path) -> list[str]:
    """Every error-severity finding, rendered as 'CODE at LOCATION: MESSAGE'."""
    report = validate_epub(path)
    return [
        f"{f.code} at {f.location}: {f.message}" for f in report.findings if f.severity == "error"
    ]


class TestReorder:
    """Tests for reorder_epub()."""

    def test_reorder_output_has_no_errors(self, tmp_path: Path, epub_fixtures: Path) -> None:
        """Reordered EPUBs validate with zero errors."""
        p = tmp_path / "r.epub"
        shutil.copy(epub_fixtures / "tending_bar.epub", p)
        # tending_bar.epub chapters are out of order; reorder_epub returns True
        assert reorder_epub(p) is True
        assert _errors(p) == []
