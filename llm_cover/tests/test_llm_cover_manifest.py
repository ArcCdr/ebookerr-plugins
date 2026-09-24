"""TDD tests for the Cover generator manifest (GEN-D1)."""

from __future__ import annotations

import re

import ebookerr_sdk.spi as p
from ebookerr_sdk.generation.client import IMAGE_APIS
from ebookerr_sdk.generation.prompt_template import PLACEHOLDERS
from llm_cover.plugin import (
    DEFAULT_IMAGE_PROMPT_TEMPLATE,
    IMAGE_MODEL_FIELDS,
    LEGACY_DEFAULT_IMAGE_PROMPT_TEMPLATE,
    LlmCoverPlugin,
)


def test_manifest_identity_and_flags() -> None:
    """Verify manifest identity (id, name, type) and core flags."""
    m = LlmCoverPlugin.manifest
    assert m.id == "llm_cover"
    assert m.name == "Cover generator"
    assert m.plugin_type is p.PluginType.BOOK
    assert m.deferred is True
    assert m.long_running is True
    assert m.network is True
    assert m.testable is True
    assert m.headed is True
    assert m.headless is True
    assert m.default_enabled is False
    assert set(m.events) == {p.PluginEventType.BOOK_CREATED, p.PluginEventType.BOOK_IMPORTED}


def test_trigger() -> None:
    """Verify the UI trigger configuration."""
    m = LlmCoverPlugin.manifest
    assert len(m.ui_triggers) == 1
    trigger = m.ui_triggers[0]
    assert trigger.scope == "book_selection_action"
    assert trigger.icon == "imagesmode"
    assert trigger.label == "Generate cover candidates"
    assert trigger.min_books == 1


def test_custom_value() -> None:
    """Verify the custom value declaration."""
    m = LlmCoverPlugin.manifest
    assert len(m.custom_values) == 1
    cv = m.custom_values[0]
    assert cv.key == "prompt_model"
    assert cv.type == "string"
    assert cv.label == "Image prompt model"
    assert cv.filterable is True


def test_top_level_settings() -> None:
    """Verify top-level settings fields, order, types, and groups."""
    schema = LlmCoverPlugin().settings_schema()
    field_tuples = [(f.key, f.type, f.group) for f in schema.fields]
    expected = [
        ("base_url", "url", "Text model"),
        ("model", "string", "Text model"),
        ("timeout_s", "int", "Text model"),
        ("input_words", "int", "Image prompt"),
        ("max_completion_tokens", "int", "Image prompt"),
        ("prompt_template", "textarea", "Image prompt"),
        ("image_models", "record_list", "Image models"),
    ]
    assert field_tuples == expected
    # base_url field has credential=True
    base_url = next(f for f in schema.fields if f.key == "base_url")
    assert base_url.credential is True
    # No field is secret
    assert not any(f.secret for f in schema.fields)


def test_image_model_row_fields() -> None:
    """Verify image model record_list sub-field definitions."""
    field_tuples = [(f.key, f.type) for f in IMAGE_MODEL_FIELDS]
    expected = [
        ("label", "string"),
        ("api", "select"),
        ("base_url", "url"),
        ("model", "string"),
        ("width", "int"),
        ("height", "int"),
        ("prompt_prefix", "string"),
        ("negative_prompt", "textarea"),
        ("extra_params", "textarea"),
        ("timeout_s", "int"),
    ]
    assert field_tuples == expected
    # api field has correct options
    api_field = next(f for f in IMAGE_MODEL_FIELDS if f.key == "api")
    assert api_field.options == ("openai", "a1111")
    assert api_field.options_strict is True
    # base_url field has credential=True
    base_url_field = next(f for f in IMAGE_MODEL_FIELDS if f.key == "base_url")
    assert base_url_field.credential is True


def test_api_options_match_the_gateway() -> None:
    """Verify api field options match the gateway's IMAGE_APIS."""
    api_field = next(f for f in IMAGE_MODEL_FIELDS if f.key == "api")
    assert api_field.options == IMAGE_APIS


def test_default_template_placeholders() -> None:
    """Verify template placeholders are all declared and contain expected content."""
    placeholders = set(re.findall(r"\{(\w+)\}", DEFAULT_IMAGE_PROMPT_TEMPLATE))
    assert placeholders.issubset(set(PLACEHOLDERS))
    assert "book_text" in placeholders
    assert "title" in placeholders
    assert "no text" in DEFAULT_IMAGE_PROMPT_TEMPLATE


def test_no_llm_in_user_copy() -> None:
    """Verify no LLM terminology in user-facing strings (GEN-D1)."""
    m = LlmCoverPlugin.manifest
    assert "LLM" not in m.name
    assert "LLM" not in m.description
    for trigger in m.ui_triggers:
        assert "LLM" not in trigger.label


def test_image_model_rows_default_to_1152_high_with_no_extra_params() -> None:
    """Verify IMAGE_MODEL_FIELDS defaults: height == 1152, extra_params == "{}"."""
    # Verify the field defaults without needing the core's decoding service
    # (the core's record-list decoding is tested in tests/unit/test_plugin_settings_service.py)
    height_field = next(f for f in IMAGE_MODEL_FIELDS if f.key == "height")
    extra_params_field = next(f for f in IMAGE_MODEL_FIELDS if f.key == "extra_params")

    assert height_field.default == 1152
    assert extra_params_field.default == "{}"


def test_cover_generator_declares_its_groups() -> None:
    """Verify the Cover generator declares its settings groups and image model summary (GEN-D21)."""
    manifest = LlmCoverPlugin.manifest
    schema = LlmCoverPlugin().settings_schema()

    # Verify groups declaration with summaries
    assert manifest.settings_schema.groups is not None
    groups_with_summaries = [(g.label, g.summary) for g in manifest.settings_schema.groups]
    expected_groups = [
        ("Text model", "{model|No model set} · {base_url|No base URL}"),
        (
            "Image prompt",
            "{input_words} words in · {max_completion_tokens|no output limit}"
            " output tokens · {prompt_template} prompt",
        ),
        ("Image models", "{image_models|No image models}"),
    ]
    assert groups_with_summaries == expected_groups
    assert all(not g.collapsed and g.description == "" for g in manifest.settings_schema.groups)

    # Verify image_models field has summary
    image_models_field = next(f for f in schema.fields if f.key == "image_models")
    assert image_models_field.summary == ("api", "width", "height")


def test_cover_generator_names_its_stages() -> None:
    """Verify the Cover generator declares stage_labels (GEN-D22)."""
    manifest = LlmCoverPlugin.manifest
    assert hasattr(manifest, "stage_labels")
    assert manifest.stage_labels == (("", "Image prompt"), ("image:", "Image"))


def test_the_default_template_starts_with_the_book_text() -> None:
    """Verify the default image template puts book text first (GEN-D23, GEN-TR-19)."""
    assert DEFAULT_IMAGE_PROMPT_TEMPLATE.startswith("Opening of the book:\n{book_text}\n\n")
    assert "based on the opening above" in DEFAULT_IMAGE_PROMPT_TEMPLATE


def test_the_legacy_default_is_a_previous_default() -> None:
    """Verify the legacy default is registered as a previous default (GEN-TR-22)."""
    schema = LlmCoverPlugin().settings_schema()
    prompt_field = next(f for f in schema.fields if f.key == "prompt_template")
    assert prompt_field.previous_defaults == (LEGACY_DEFAULT_IMAGE_PROMPT_TEMPLATE,)
    assert LEGACY_DEFAULT_IMAGE_PROMPT_TEMPLATE.endswith("Opening of the book:\n{book_text}")
