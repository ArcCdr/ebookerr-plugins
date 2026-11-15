"""Tests for the Cover generator's cover guard on automatic chains (``GEN-D8``)."""

from __future__ import annotations

import base64
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import responses
from ebookerr_sdk.spi import AssetView, BookView, PluginEventType
from ebookerr_sdk.testing import FakeContext, make_book_view
from llm_cover.plugin import LlmCoverPlugin
from PIL import Image

_COVER_SKIP = "the book already has a cover"


def _png() -> bytes:
    """Create a minimal 2x2 PNG."""
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color="blue").save(buffer, format="PNG")
    return buffer.getvalue()


def _answer_reachability_checks() -> None:
    """Answer the reachability check every generation call makes first."""
    responses.add(responses.GET, re.compile(r"^https?://[^/]+(/.*)?/models$"), json={"data": []})
    responses.add(responses.GET, re.compile(r"^https?://[^/]+/sdapi/v1/options$"), json={})


def _ctx(
    tmp_path: Path, ui_context: dict[str, str], event_type: PluginEventType | None = None
) -> FakeContext:
    """Build a context with one image model, the given UI context and event type."""
    ctx = FakeContext(
        settings={
            "base_url": "http://llm.test/v1",
            "model": "qwen3:8b",
            "timeout_s": 30,
            "input_words": 50,
            "max_completion_tokens": 0,
            "prompt_template": "Draw {title}: {book_text}",
            "image_models": [
                {
                    "label": "m",
                    "api": "a1111",
                    "base_url": "http://img.test:7860",
                    "model": "",
                    "width": 768,
                    "height": 1152,
                    "prompt_prefix": "",
                    "negative_prompt": "",
                    "extra_params": "{}",
                    "timeout_s": 60,
                }
            ],
        },
        event_type=event_type,
        library_root=tmp_path,
    )
    ctx.ui_context = ui_context
    return ctx


def _view(tmp_path: Path, *, cover_ref: str | None) -> BookView:
    """Build a book that holds a stored image prompt and the given cover."""
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("lighthouse at dusk")
    return make_book_view(
        book_id="b1",
        title="T",
        output_filename="book.epub",
        cover_ref=cover_ref,
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(prompt),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )


def _mock_image_endpoint() -> None:
    """Answer the image endpoint with one PNG."""
    _answer_reachability_checks()
    responses.add(
        responses.POST,
        "http://img.test:7860/sdapi/v1/txt2img",
        json={"images": [base64.b64encode(_png()).decode("ascii")]},
    )
    responses.add(
        responses.GET,
        "http://img.test:7860/sdapi/v1/progress",
        json={"progress": 1.0, "current_image": None},
    )


@responses.activate
def test_automatic_image_step_skips_a_book_that_gained_a_cover(tmp_path: Path) -> None:
    """An image step of an automatic chain draws nothing for a book with a cover."""
    ctx = _ctx(tmp_path, {"stage": "image:m", "automatic": "1"})

    patches = LlmCoverPlugin().enrich((_view(tmp_path, cover_ref="covers/c.jpg"),), ctx)

    assert patches == []
    assert len(responses.calls) == 0
    assert ctx.skips == [("b1", _COVER_SKIP)]


@responses.activate
def test_manual_image_step_generates_despite_a_cover(tmp_path: Path) -> None:
    """An image step started by hand still draws when the book has a cover."""
    _mock_image_endpoint()
    ctx = _ctx(tmp_path, {"stage": "image:m"})

    patches = LlmCoverPlugin().enrich((_view(tmp_path, cover_ref="covers/c.jpg"),), ctx)

    assert len(patches) == 1
    assert patches[0].assets[0].kind == "cover_candidate"
    assert ctx.skips == []


@pytest.mark.parametrize(
    "ui_context", [{"stage": "image:m"}, {"stage": "image:m", "automatic": "1"}]
)
@responses.activate
def test_image_step_generates_for_a_book_without_a_cover(
    tmp_path: Path, build_epub: Any, ui_context: dict[str, str]
) -> None:
    """An image step draws for a book with no cover, automatic or not."""
    _mock_image_endpoint()
    build_epub([("Chapter 1", "https://x/1")])
    ctx = _ctx(tmp_path, ui_context)

    patches = LlmCoverPlugin().enrich((_view(tmp_path, cover_ref=None),), ctx)

    assert len(patches) == 1
    assert ctx.skips == []


@responses.activate
def test_entry_step_reads_the_automatic_flag_without_an_event(
    tmp_path: Path, build_epub: Any
) -> None:
    """An entry run carrying only the automatic flag skips a book with a cover."""
    build_epub([("Chapter 1", "https://x/1")])
    ctx = _ctx(tmp_path, {"automatic": "1"})

    patches = LlmCoverPlugin().enrich((_view(tmp_path, cover_ref="covers/c.jpg"),), ctx)

    assert patches == []
    assert len(responses.calls) == 0
    assert ctx.skips == [("b1", _COVER_SKIP)]
