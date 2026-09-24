"""TDD tests for the Synopsis generator manifest (GEN-D1)."""

from __future__ import annotations

import re

import ebookerr_sdk.spi as p
from ebookerr_sdk.generation.prompt_template import PLACEHOLDERS
from llm_synopsis.plugin import (
    DEFAULT_SYNOPSIS_TEMPLATE,
    LEGACY_DEFAULT_SYNOPSIS_TEMPLATE,
    LlmSynopsisPlugin,
)


def test_manifest_identity_and_flags() -> None:
    """Verify manifest identity (id, name, type) and core flags."""
    m = LlmSynopsisPlugin.manifest
    assert m.id == "llm_synopsis"
    assert m.name == "Synopsis generator"
    assert m.plugin_type is p.PluginType.BOOK
    assert m.deferred is True
    assert m.long_running is True
    assert m.network is True
    assert m.testable is True
    assert m.headed is True
    assert m.default_enabled is False
    assert set(m.events) == {p.PluginEventType.BOOK_CREATED, p.PluginEventType.BOOK_IMPORTED}


def test_trigger() -> None:
    """Verify the UI trigger configuration."""
    m = LlmSynopsisPlugin.manifest
    assert len(m.ui_triggers) == 1
    trigger = m.ui_triggers[0]
    assert trigger.scope == "book_selection_action"
    assert trigger.icon == "auto_awesome"
    assert trigger.label == "Generate synopsis"


def test_custom_value() -> None:
    """Verify the custom value declaration."""
    m = LlmSynopsisPlugin.manifest
    assert len(m.custom_values) == 1
    cv = m.custom_values[0]
    assert cv.key == "model"
    assert cv.type == "string"
    assert cv.label == "Synopsis model"
    assert cv.filterable is True


def test_settings_fields_and_groups() -> None:
    """Verify settings fields, order, types, and groups."""
    schema = LlmSynopsisPlugin().settings_schema()
    field_tuples = [(f.key, f.type, f.group) for f in schema.fields]
    expected = [
        ("base_url", "url", "Text model"),
        ("model", "string", "Text model"),
        ("timeout_s", "int", "Text model"),
        ("input_words", "int", "Generation"),
        ("max_completion_tokens", "int", "Generation"),
        ("prompt_template", "textarea", "Generation"),
    ]
    assert field_tuples == expected
    # base_url field has credential=True
    base_url = next(f for f in schema.fields if f.key == "base_url")
    assert base_url.credential is True
    # No field is secret
    assert not any(f.secret for f in schema.fields)


def test_default_template_uses_only_known_placeholders() -> None:
    """Verify template placeholders are all declared."""
    placeholders = set(re.findall(r"\{(\w+)\}", DEFAULT_SYNOPSIS_TEMPLATE))
    assert placeholders.issubset(set(PLACEHOLDERS))
    assert "book_text" in placeholders


def test_manifest_names_no_llm_in_user_copy() -> None:
    """Verify no LLM terminology in user-facing strings (GEN-D1)."""
    m = LlmSynopsisPlugin.manifest
    assert "LLM" not in m.name
    assert "LLM" not in m.description
    for trigger in m.ui_triggers:
        assert "LLM" not in trigger.label


def test_synopsis_generator_declares_its_groups() -> None:
    """Verify the Synopsis generator declares its settings groups (GEN-D21)."""
    manifest = LlmSynopsisPlugin.manifest

    # Verify groups declaration with summaries
    assert manifest.settings_schema.groups is not None
    groups_with_summaries = [(g.label, g.summary) for g in manifest.settings_schema.groups]
    expected_groups = [
        ("Text model", "{model|No model set} · {base_url|No base URL}"),
        (
            "Generation",
            "{input_words} words in · {max_completion_tokens|no output limit}"
            " output tokens · {prompt_template} prompt",
        ),
    ]
    assert groups_with_summaries == expected_groups
    assert all(not g.collapsed and g.description == "" for g in manifest.settings_schema.groups)


def test_the_default_template_starts_with_the_book_text() -> None:
    """Verify the default template puts book text first (GEN-D23, GEN-TR-19)."""
    assert DEFAULT_SYNOPSIS_TEMPLATE.startswith("Opening of the book:\n{book_text}\n\n")
    assert "based on the opening above" in DEFAULT_SYNOPSIS_TEMPLATE


def test_the_legacy_default_is_a_previous_default() -> None:
    """Verify the legacy default is registered as a previous default (GEN-TR-22)."""
    schema = LlmSynopsisPlugin().settings_schema()
    prompt_field = next(f for f in schema.fields if f.key == "prompt_template")
    assert prompt_field.previous_defaults == (LEGACY_DEFAULT_SYNOPSIS_TEMPLATE,)
    assert LEGACY_DEFAULT_SYNOPSIS_TEMPLATE.endswith("Opening of the book:\n{book_text}")
