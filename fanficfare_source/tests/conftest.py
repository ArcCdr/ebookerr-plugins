"""Pytest configuration for fanficfare_source plugin tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fanficfare_fixtures() -> Path:
    """Directory of real captured FanFicFare CLI stdout/stderr."""
    return FIXTURES_DIR
