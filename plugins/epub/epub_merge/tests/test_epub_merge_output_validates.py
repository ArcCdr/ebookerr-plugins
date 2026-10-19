"""Regression net: a merged EPUB must carry no integrity findings (``§C.8``)."""

from __future__ import annotations

import shutil
from pathlib import Path

from ebookerr_sdk.epub.integrity import inspect_epub
from epub_merge.merge.merge import merge_epubs

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_alpha.epub"
_FIXTURE2 = Path(__file__).resolve().parent / "fixtures" / "Gamma35.epub"


class TestMerge:
    """Tests for merge_epubs()."""

    def test_merge_output_has_no_findings(self, tmp_path: Path) -> None:
        """Merged EPUBs carry no integrity findings."""
        m = tmp_path / "m.epub"
        s = tmp_path / "s.epub"
        shutil.copy(_FIXTURE, m)
        shutil.copy(_FIXTURE2, s)
        # merge_epubs modifies target in place and returns MergeOutcome
        assert merge_epubs(m, [s]).chapter_count == 7
        assert inspect_epub(m).findings == ()

    def test_merge_output_is_epub3_with_a_nav_document(self, tmp_path: Path) -> None:
        """Merged output is EPUB3 with nav.xhtml outside the spine.

        It is sound by the integrity kit.
        """
        m = tmp_path / "m.epub"
        s = tmp_path / "s.epub"
        shutil.copy(_FIXTURE, m)
        shutil.copy(_FIXTURE2, s)
        merge_epubs(m, [s])
        assert inspect_epub(m).findings == ()
