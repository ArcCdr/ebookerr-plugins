"""Regression net: EPUB output from normalize_epub() is sound by the integrity kit."""

from __future__ import annotations

import shutil
from pathlib import Path

from ebookerr_sdk.epub.integrity import inspect_epub
from epub_normalize.normalize import NormalizeOptions, normalize_epub

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_alpha.epub"


class TestNormalize:
    """Tests for normalize_epub()."""

    def test_normalize_output_has_no_findings(self, tmp_path: Path) -> None:
        """Normalized EPUBs carry no integrity findings."""
        p = tmp_path / "n.epub"
        shutil.copy(_FIXTURE, p)
        normalize_epub(p, options=NormalizeOptions())
        assert inspect_epub(p).findings == ()

    def test_normalize_output_is_still_sound(self, tmp_path: Path) -> None:
        """Normalized EPUBs remain sound by the integrity kit."""
        p = tmp_path / "n.epub"
        shutil.copy(_FIXTURE, p)
        normalize_epub(p, options=NormalizeOptions())
        assert inspect_epub(p).findings == ()
