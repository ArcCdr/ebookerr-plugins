"""TDD tests for the Cover generator's model field choices (SPI 3.12)."""

from __future__ import annotations

from typing import Any

import pytest
from ebookerr_sdk.spi import SettingOptions
from llm_cover import plugin
from llm_cover.plugin import LlmCoverPlugin


def test_the_text_model_lists_the_openai_endpoints_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text Model field lists the OpenAI endpoint's models through model_options."""
    ctx = object()
    recorder: list[tuple[str, str, Any]] = []

    def fake_model_options(api: str, base_url: str, ctx_: Any) -> SettingOptions:
        recorder.append((api, base_url, ctx_))
        return SettingOptions(options=("m1",))

    monkeypatch.setattr(plugin, "model_options", fake_model_options)
    result = LlmCoverPlugin().setting_options("model", {"base_url": "http://h.test/v1"}, ctx)
    assert result == SettingOptions(options=("m1",))
    assert recorder == [("openai", "http://h.test/v1", ctx)]


def test_an_image_model_lists_its_rows_server_by_its_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The image Model sub-field lists its row's endpoint models by the row's api."""
    ctx = object()
    recorder: list[tuple[str, str, Any]] = []

    def fake_model_options(api: str, base_url: str, ctx_: Any) -> SettingOptions:
        recorder.append((api, base_url, ctx_))
        return SettingOptions(options=("m1",))

    monkeypatch.setattr(plugin, "model_options", fake_model_options)
    result = LlmCoverPlugin().setting_options(
        "image_models.model", {"api": "a1111", "base_url": "http://i.test"}, ctx
    )
    assert result == SettingOptions(options=("m1",))
    assert recorder == [("a1111", "http://i.test", ctx)]


def test_another_field_lists_nothing() -> None:
    """A field other than the two Model fields lists no choices."""
    ctx = object()
    result = LlmCoverPlugin().setting_options("timeout_s", {}, ctx)
    assert result.problem == "This field lists no choices."
