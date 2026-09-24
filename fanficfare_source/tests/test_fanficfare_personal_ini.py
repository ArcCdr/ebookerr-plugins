"""FanFicFare configuration seeding and BOM stripping (D37, TXE-D1)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_personal_ini_is_seeded_on_first_use_without_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration file is seeded from the package default on first use,
    and any UTF-8 BOM is stripped, but user edits are preserved."""
    from fanficfare_source.plugin import personal_ini_path

    monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "fanficfare_source"))
    path = personal_ini_path()

    # Check the file was created at the expected location
    assert path == tmp_path / "fanficfare_source" / "personal.ini"
    assert path.is_file()

    # Verify the packaged default was copied
    packaged = Path(__file__).resolve().parents[1] / "fanficfare_source" / "personal.ini"
    assert packaged.is_file()
    assert path.read_bytes() == packaged.read_bytes()

    # Second call should not overwrite (no BOM present)
    path.write_bytes(b"[defaults]\nis_adult:true\n")
    path = personal_ini_path()
    assert path.read_bytes() == b"[defaults]\nis_adult:true\n"

    # But a file with a BOM should have it stripped
    monkeypatch.setenv("EBOOKERR_PLUGIN_DATA_DIR", str(tmp_path / "fanficfare_bom"))
    path_bom = personal_ini_path()
    path_bom.write_bytes(b"\xef\xbb\xbf[defaults]\n")
    path_bom = personal_ini_path()
    assert path_bom.read_bytes() == b"[defaults]\n"
