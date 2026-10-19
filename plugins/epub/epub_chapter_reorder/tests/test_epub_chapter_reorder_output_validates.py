"""Regression net: EPUB output from reorder_epub() is sound by the integrity kit."""

from __future__ import annotations

import shutil
from pathlib import Path

from ebookerr_sdk.epub.integrity import inspect_epub
from epub_chapter_reorder.reorder_step import reorder_epub


class TestReorder:
    """Tests for reorder_epub()."""

    def test_reorder_output_has_no_findings(self, tmp_path: Path, epub_fixtures: Path) -> None:
        """Reordered EPUBs carry no integrity findings."""
        p = tmp_path / "r.epub"
        shutil.copy(epub_fixtures / "sample_alpha.epub", p)
        # sample_alpha.epub chapters are out of order; reorder_epub returns True
        assert reorder_epub(p) is True
        assert inspect_epub(p).findings == ()
