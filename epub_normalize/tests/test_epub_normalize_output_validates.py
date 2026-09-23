"""Regression net: EPUB output from normalize_epub() validates with zero errors."""

from __future__ import annotations

import shutil
from pathlib import Path

from ebookerr_sdk.validate import validate_epub
from epub_normalize.normalize import NormalizeOptions, normalize_epub

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tending_bar.epub"


def _errors(path: Path) -> list[str]:
    """Every error-severity finding, rendered as 'CODE at LOCATION: MESSAGE'."""
    report = validate_epub(path)
    return [
        f"{f.code} at {f.location}: {f.message}" for f in report.findings if f.severity == "error"
    ]


class TestNormalize:
    """Tests for normalize_epub()."""

    def test_normalize_output_has_no_errors(self, tmp_path: Path) -> None:
        """Normalized EPUBs validate with zero errors."""
        p = tmp_path / "n.epub"
        shutil.copy(_FIXTURE, p)
        normalize_epub(p, options=NormalizeOptions())
        assert _errors(p) == []

    def test_normalize_output_is_still_valid(self, tmp_path: Path) -> None:
        """Normalized EPUBs retain valid status."""
        p = tmp_path / "n.epub"
        shutil.copy(_FIXTURE, p)
        normalize_epub(p, options=NormalizeOptions())
        assert validate_epub(p).status == "valid"
