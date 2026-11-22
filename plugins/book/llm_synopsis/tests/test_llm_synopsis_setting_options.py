"""TDD tests for the Synopsis generator's model field choices (SPI 3.12)."""

from __future__ import annotations

from typing import Any

import pytest
from ebookerr_sdk.spi import SettingOptions
from llm_synopsis import plugin
from llm_synopsis.plugin import LlmSynopsisPlugin


def test_the_model_field_lists_the_endpoints_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Model field lists the endpoint's models through model_options."""
    ctx = object()
    recorder: list[tuple[str, str, Any]] = []

    def fake_model_options(api: str, base_url: str, ctx_: Any) -> SettingOptions:
        recorder.append((api, base_url, ctx_))
        return SettingOptions(options=("m1",))

    monkeypatch.setattr(plugin, "model_options", fake_model_options)
    result = LlmSynopsisPlugin().setting_options(
        "model", {"base_url": "http://h.test/v1"}, ctx
    )
    assert result == SettingOptions(options=("m1",))
    assert recorder == [("openai", "http://h.test/v1", ctx)]


def test_another_field_lists_nothing() -> None:
    """A field other than Model lists no choices."""
    ctx = object()
    result = LlmSynopsisPlugin().setting_options("timeout_s", {}, ctx)
    assert result.problem == "This field lists no choices."
