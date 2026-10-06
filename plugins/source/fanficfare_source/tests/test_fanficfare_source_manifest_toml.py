"""The staged manifest states exactly what the plugin class declares (PMG-D24)."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_the_manifest_declares_the_fallback_source_role() -> None:
    """The manifest declares the fallback_source role and SPI 3.0, no legacy catch_all."""
    manifest_path = Path(__file__).parent.parent / "manifest.toml"
    with manifest_path.open("rb") as f:
        data = tomllib.load(f)

    assert data["roles"] == {"fallback_source": {}}
    assert "catch_all" not in data
    assert data["spi_version"] == "3.0"


def test_the_source_answers_update_checks_and_is_the_catch_all() -> None:
    """The manifest declares update_check and fallback_source role, and fanficfare requirement."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    assert FanFicFareSourcePlugin.manifest.update_check is True
    assert FanFicFareSourcePlugin.manifest.roles.fallback_source is True
    assert FanFicFareSourcePlugin.manifest.requirements == ("fanficfare>=4.62.0",)


def test_the_source_is_version_1_4_0() -> None:
    """The 2.22.1 release of the FanFicFare Source is 1.4.0."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    assert FanFicFareSourcePlugin.manifest.version == "1.4.0"


def test_the_manifest_declares_its_fanficfare_settings() -> None:
    """The manifest declares six FanFicFare settings (C36)."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    schema = FanFicFareSourcePlugin.manifest.settings_schema
    assert len(schema.fields) == 6

    keys = [f.key for f in schema.fields]
    assert keys == [
        "is_adult",
        "include_images",
        "keep_summary_html",
        "make_firstimage_cover",
        "include_subject_tags",
        "extra_options",
    ]

    types = [f.type for f in schema.fields]
    assert types == ["bool", "bool", "bool", "bool", "string", "textarea"]

    # Check specific fields
    assert schema.fields[0].label == "Confirm adult content"
    assert schema.fields[0].default is True
    assert schema.fields[0].help == "Answer yes when a site asks you to confirm you are an adult."

    assert schema.fields[1].label == "Include images"
    assert schema.fields[1].default is True
    assert schema.fields[1].help == "Download the images inside chapters and the summary."

    assert schema.fields[2].label == "Keep the summary's formatting"
    assert schema.fields[2].default is True
    assert (
        schema.fields[2].help
        == "Keep links, emphasis and images in the summary instead of plain text."
    )

    assert schema.fields[3].label == "Use the first image as the cover"
    assert schema.fields[3].default is True
    assert (
        schema.fields[3].help == "When a story has images, the first one becomes the book's cover."
    )

    assert schema.fields[4].label == "Tags to keep"
    tags_default = "extratags, genre, category, characters, ships, lastupdate, status"
    assert schema.fields[4].default == tags_default
    assert (
        schema.fields[4].help
        == "The story details written as the book's tags, separated by commas."
    )

    assert schema.fields[5].label == "Advanced FanFicFare options"
    assert schema.fields[5].default == ""
    advanced_help = (
        "More FanFicFare settings in its own format, for example a [www.example.com] "
        "section. Sign-ins belong in Settings → Credentials; ebookerr chooses file "
        "names and folders."
    )
    assert schema.fields[5].help == advanced_help


def test_the_source_receives_the_sign_in_of_any_site() -> None:
    """The manifest declares auth_sites = ["*"] (C36)."""
    from fanficfare_source.plugin import FanFicFareSourcePlugin

    assert FanFicFareSourcePlugin.manifest.auth_sites == ("*",)
