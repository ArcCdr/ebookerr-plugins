"""TDD tests for the Cover generator entry run (GEN-D7, GEN-D8, GEN-FR-2, GEN-FR-16, GEN-TR-18)."""

from __future__ import annotations

import base64
import json
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import requests
import responses
from ebookerr_sdk.spi import (
    AssetView,
    BookView,
    ExternalLink,
    ExternalProgress,
    PluginEventType,
)
from ebookerr_sdk.testing import FakeContext
from llm_cover.plugin import LlmCoverPlugin, _has_cover, _read_text
from PIL import Image


def _sse(*objs: dict[str, Any], done: bool = True) -> str:
    """Construct SSE stream text from JSON objects."""
    lines = [f"data: {json.dumps(o)}\n\n" for o in objs]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _create_test_jpeg() -> bytes:
    """Create a minimal 2x2 JPEG for testing."""
    img = Image.new("RGB", (2, 2), color="red")
    buffer = BytesIO()
    img.save(buffer, format="JPEG")
    return buffer.getvalue()


def _answer_reachability_checks() -> None:
    """Answer the reachability check every generation call makes first (``GEN-D24``).

    Registers a 200 for any ``GET …/models`` and ``GET …/sdapi/v1/options``; call it first inside a
    ``@responses.activate`` test that makes a generation call.
    """
    responses.add(responses.GET, re.compile(r"^https?://[^/]+(/.*)?/models$"), json={"data": []})
    responses.add(responses.GET, re.compile(r"^https?://[^/]+/sdapi/v1/options$"), json={})


def _posts() -> list[Any]:
    """The recorded POST calls, leaving out the reachability checks' GETs."""
    return [c for c in responses.calls if c.request.method == "POST"]


@pytest.fixture
def ctx(tmp_path: Path) -> FakeContext:
    """A FakeContext for cover generation tests."""
    settings: dict[str, Any] = {
        "base_url": "http://llm.test/v1",
        "model": "qwen3:8b",
        "timeout_s": 30,
        "input_words": 50,
        "max_completion_tokens": 0,
        "prompt_template": "Draw {title}: {book_text}",
        "image_models": [
            {
                "label": "flux-dev",
                "api": "a1111",
                "base_url": "http://img.test:7860",
                "model": "",
                "width": 768,
                "height": 1152,
                "prompt_prefix": "",
                "negative_prompt": "",
                "extra_params": "{}",
                "timeout_s": 60,
            },
            {
                "label": "sdxl",
                "api": "a1111",
                "base_url": "http://img.test:7860",
                "model": "",
                "width": 768,
                "height": 1152,
                "prompt_prefix": "",
                "negative_prompt": "",
                "extra_params": "{}",
                "timeout_s": 60,
            },
        ],
    }
    return FakeContext(
        settings=settings,
        event_type=PluginEventType.BOOK_CREATED,
        library_root=tmp_path,
    )


@pytest.fixture
def view() -> BookView:
    """A minimal BookView for cover generation tests."""
    return BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
    )


@responses.activate
def test_entry_generates_prompt_and_queues_every_model(
    build_epub: Any,
    ctx: FakeContext,
    view: BookView,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entry run generates prompt and queues image models; returns one patch."""
    _answer_reachability_checks()
    caplog.set_level(logging.INFO)
    build_epub([("Chapter 1", "https://x/1")])

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse(
            {"choices": [{"delta": {"content": "lighthouse at "}}]},
            {"choices": [{"delta": {"content": "dusk, oil painting"}}]},
        ),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    patch = patches[0]
    assert len(patch.assets) == 1
    asset = patch.assets[0]
    assert asset.kind == "t2i_prompt"
    assert asset.name == "qwen3-8b"
    assert asset.data == b"lighthouse at dusk, oil painting"
    assert asset.media_type == "text/plain"
    assert asset.meta["model"] == "qwen3:8b"
    assert "generated_at" in asset.meta
    assert patch.custom_values["prompt_model"].value == "qwen3:8b"
    assert ctx.deferrals == [("b1", "image:flux-dev"), ("b1", "image:sdxl")]
    assert 'Image prompt generated for "T" (book_id=b1): model=qwen3:8b in' in caplog.text
    assert (
        'Image generation queued for "T" (book_id=b1): 2 model(s), 0 already present' in caplog.text
    )


@responses.activate
def test_entry_reuses_an_existing_prompt(build_epub: Any, ctx: FakeContext) -> None:
    """Entry run reuses existing prompt; no HTTP call, no patch."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(ctx.library_root / "p.txt"),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.deferrals == [("b1", "image:flux-dev"), ("b1", "image:sdxl")]
    assert len(responses.calls) == 0


@responses.activate
def test_entry_skips_models_whose_candidate_exists(
    build_epub: Any,
    ctx: FakeContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entry run skips queueing for models with existing candidates."""
    _answer_reachability_checks()
    caplog.set_level(logging.INFO)
    build_epub([("Chapter 1", "https://x/1")])
    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="cover_candidate",
                name="flux-dev",
                path=str(ctx.library_root / "c1.jpg"),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "test"}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    assert ctx.deferrals == [("b1", "image:sdxl")]
    assert (
        'Image generation queued for "T" (book_id=b1): 1 model(s), 1 already present' in caplog.text
    )


@responses.activate
def test_automatic_run_skips_a_book_with_a_confirmed_cover(
    build_epub: Any,
    ctx: FakeContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Automatic run skips book with cover_ref set."""
    _answer_reachability_checks()
    caplog.set_level(logging.INFO)
    build_epub([("Chapter 1", "https://x/1")])
    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref="/lib/cover.png",
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.deferrals == []
    assert ctx.skips == [("b1", "the book already has a cover")]
    assert "the book already has a cover" in caplog.text


@responses.activate
def test_automatic_run_skips_a_book_with_an_embedded_cover(
    build_epub: Any,
    ctx: FakeContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Automatic run skips book with embedded cover image."""
    _answer_reachability_checks()
    caplog.set_level(logging.INFO)
    build_epub([("Chapter 1", "https://x/1")], cover=_create_test_jpeg())
    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.deferrals == []
    assert ctx.skips == [("b1", "the book already has a cover")]


@responses.activate
def test_manual_run_ignores_the_cover(build_epub: Any, ctx: FakeContext) -> None:
    """Manual run (BOOK_UPDATED) ignores existing cover."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")], cover=_create_test_jpeg())
    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref="/lib/cover.png",
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
    )
    ctx.event_type = PluginEventType.BOOK_UPDATED

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "test"}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    assert ctx.deferrals == [("b1", "image:flux-dev"), ("b1", "image:sdxl")]


@responses.activate
def test_no_image_models_skips(
    build_epub: Any,
    ctx: FakeContext,
    view: BookView,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entry run skips when no image models are configured."""
    _answer_reachability_checks()
    caplog.set_level(logging.INFO)
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["image_models"] = []

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "test"}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.deferrals == []
    assert ctx.skips == [("b1", "no image models are configured")]
    assert len(responses.calls) == 0


@responses.activate
def test_duplicate_labels_keep_the_first(
    build_epub: Any,
    ctx: FakeContext,
    view: BookView,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entry run deduplicates by label (case-sensitive model_key)."""
    _answer_reachability_checks()
    caplog.set_level(logging.WARNING)
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["image_models"] = [
        {
            "label": "flux-dev",
            "api": "a1111",
            "base_url": "http://img.test:7860",
            "model": "",
            "width": 768,
            "height": 1152,
            "prompt_prefix": "",
            "negative_prompt": "",
            "extra_params": "{}",
            "timeout_s": 60,
        },
        {
            "label": "Flux Dev",  # Different label but same key after model_key normalization
            "api": "a1111",
            "base_url": "http://img.test:7860",
            "model": "",
            "width": 768,
            "height": 1152,
            "prompt_prefix": "",
            "negative_prompt": "",
            "extra_params": "{}",
            "timeout_s": 60,
        },
    ]

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "test"}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    assert ctx.deferrals == [("b1", "image:flux-dev")]
    assert "Image model label" in caplog.text
    assert "repeats; the later row is ignored" in caplog.text


@responses.activate
def test_missing_text_model_holds(build_epub: Any, ctx: FakeContext, view: BookView) -> None:
    """Entry run holds when text model is empty."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["model"] = ""

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.skips) == 1
    assert "on hold: set the text model's Model in the Cover generator settings" in ctx.skips[0][1]


@responses.activate
def test_unknown_stage_is_skipped(build_epub: Any, ctx: FakeContext, view: BookView) -> None:
    """Entry run with unknown stage is skipped."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.ui_context = {"stage": "weird"}

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.skips) == 1
    assert "unknown stage weird" in ctx.skips[0][1]


def _create_test_png() -> bytes:
    """Create a minimal 2x2 PNG for testing."""
    img = Image.new("RGB", (2, 2), color="blue")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@responses.activate
def test_image_run_writes_a_candidate(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Image run generates a candidate from prompt and returns a patch."""
    _answer_reachability_checks()
    caplog.set_level(logging.INFO)
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("lighthouse at dusk")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Mock image generation endpoint
    png_data = _create_test_png()
    responses.add(
        responses.POST,
        "http://img.test:7860/sdapi/v1/txt2img",
        json={"images": [base64.b64encode(png_data).decode("ascii")]},
        status=200,
    )
    # Mock progress polling endpoint
    responses.add(
        responses.GET,
        "http://img.test:7860/sdapi/v1/progress",
        json={"progress": 1.0, "current_image": None},
        status=200,
    )

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    patch = patches[0]
    assert len(patch.assets) == 1
    asset = patch.assets[0]
    assert asset.kind == "cover_candidate"
    assert asset.name == "flux-dev"
    assert asset.media_type == "image/png"
    assert asset.data == png_data
    assert asset.meta["label"] == "flux-dev"
    assert asset.meta["api"] == "a1111"
    assert patch.fields == {}

    # Verify the request body
    assert len(_posts()) == 1
    request_body = json.loads(_posts()[0].request.body)
    assert request_body["prompt"] == "lighthouse at dusk"

    assert 'Cover candidate generated for "T" (book_id=b1): label=flux-dev model=-,' in caplog.text


@responses.activate
def test_image_run_applies_prompt_prefix_placeholders(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run applies prompt prefix with placeholder substitution."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("lighthouse at dusk")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        category="Mystery",
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Mock image generation endpoint
    png_data = _create_test_png()
    responses.add(
        responses.POST,
        "http://img.test:7860/sdapi/v1/txt2img",
        json={"images": [base64.b64encode(png_data).decode("ascii")]},
        status=200,
    )

    # Update image model to include prefix
    ctx.settings["image_models"][0]["prompt_prefix"] = "{category} cover,"
    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    request_body = json.loads(_posts()[0].request.body)
    assert request_body["prompt"] == "Mystery cover, lighthouse at dusk"


@responses.activate
def test_image_run_reports_note(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run reports progress note."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Mock image generation endpoint
    png_data = _create_test_png()
    responses.add(
        responses.POST,
        "http://img.test:7860/sdapi/v1/txt2img",
        json={"images": [base64.b64encode(png_data).decode("ascii")]},
        status=200,
    )

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    # Check that report was called with the correct note
    assert (0.0, "Generating image · flux-dev", "") in ctx.reports


@responses.activate
def test_image_run_missing_prompt_requeues_the_entry(
    build_epub: Any,
    ctx: FakeContext,
) -> None:
    """Image run with missing prompt skips and requeues the entry stage."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # No prompt asset
    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(),
    )

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.skips == [("b1", "the image prompt is missing")]
    assert ctx.deferrals == [("b1", "")]


@responses.activate
def test_image_run_unconfigured_label_skips(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run with unconfigured label skips."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Use a stage with a model key that's not configured
    ctx.ui_context = {"stage": "image:gone"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.skips == [("b1", "image model gone is no longer configured")]


@responses.activate
def test_image_run_existing_candidate_skips(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run with existing candidate skips without making HTTP call."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
            AssetView(
                kind="cover_candidate",
                name="flux-dev",
                path=str(tmp_path / "candidate.png"),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.skips == [("b1", "a candidate from flux-dev already exists")]
    assert len(responses.calls) == 0


@responses.activate
def test_image_run_invalid_extra_params_holds(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run with invalid JSON in extra_params holds configuration."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Set invalid extra_params
    ctx.settings["image_models"][0]["extra_params"] = "[1]"
    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.skips) == 1
    assert "on hold:" in ctx.skips[0][1]
    assert "fix Extra parameters of image model flux-dev" in ctx.skips[0][1]
    # Verify the hold was placed by checking holds
    assert any(key == "config:llm_cover" for key, _ in ctx.circuit.holds)


@responses.activate
def test_image_run_unreachable_is_retryable(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run with ConnectionError reports retryable failure."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Mock connection error
    responses.add(
        responses.POST,
        "http://img.test:7860/sdapi/v1/txt2img",
        body=requests.ConnectionError("Connection refused"),
    )

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.failures) == 1
    assert ctx.failures[0][0] == "b1"
    assert "img.test:7860 is not reachable" in ctx.failures[0][1]
    assert ctx.failures[0][2] is True  # retryable


@responses.activate
def test_image_run_not_an_image_is_non_retryable(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run with non-image bytes reports non-retryable failure."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Mock image generation endpoint returning non-image data
    bad_data = base64.b64encode(b"hello").decode("ascii")
    responses.add(
        responses.POST,
        "http://img.test:7860/sdapi/v1/txt2img",
        json={"images": [bad_data]},
        status=200,
    )

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.failures) == 1
    assert ctx.failures[0][0] == "b1"
    assert "not an image" in ctx.failures[0][1]
    assert ctx.failures[0][2] is False  # not retryable


@responses.activate
def test_image_run_never_selects_the_cover(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run never sets cover_ref or selected_cover in patch."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Mock image generation endpoint
    png_data = _create_test_png()
    responses.add(
        responses.POST,
        "http://img.test:7860/sdapi/v1/txt2img",
        json={"images": [base64.b64encode(png_data).decode("ascii")]},
        status=200,
    )

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    patch = patches[0]
    assert "cover_ref" not in patch.fields
    assert "selected_cover" not in patch.fields


# Test connection tests


@responses.activate
def test_test_connection_all_ok(tmp_path: Path) -> None:
    """test_connection succeeds when text and image endpoints answer."""
    settings: dict[str, Any] = {
        "base_url": "http://llm.test/v1",
        "model": "qwen3:8b",
        "image_models": [
            {
                "label": "flux-dev",
                "api": "a1111",
                "base_url": "http://img.test:7860",
            }
        ],
    }

    responses.add(
        responses.GET,
        "http://llm.test/v1/models",
        json={"data": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "http://img.test:7860/sdapi/v1/options",
        json={},
        status=200,
    )

    ctx = FakeContext(settings=settings, library_root=tmp_path)
    ok, message = LlmCoverPlugin().test_connection(ctx)

    assert ok is True
    assert message == "Text model: llm.test answered: 0 model(s) · flux-dev: img.test:7860 answered"


@responses.activate
def test_test_connection_one_image_endpoint_down(tmp_path: Path) -> None:
    """test_connection fails when an image endpoint is unreachable."""
    settings: dict[str, Any] = {
        "base_url": "http://llm.test/v1",
        "model": "qwen3:8b",
        "image_models": [
            {
                "label": "flux-dev",
                "api": "a1111",
                "base_url": "http://img.test:7860",
            }
        ],
    }

    responses.add(
        responses.GET,
        "http://llm.test/v1/models",
        json={"data": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "http://img.test:7860/sdapi/v1/options",
        body=requests.ConnectionError("Connection refused"),
    )

    ctx = FakeContext(settings=settings, library_root=tmp_path)
    ok, message = LlmCoverPlugin().test_connection(ctx)

    assert ok is False
    assert "flux-dev: img.test:7860 is not reachable" in message


@responses.activate
def test_test_connection_no_image_models(tmp_path: Path) -> None:
    """test_connection fails when no image models are configured."""
    settings: dict[str, Any] = {
        "base_url": "http://llm.test/v1",
        "model": "qwen3:8b",
        "image_models": [],
    }

    responses.add(
        responses.GET,
        "http://llm.test/v1/models",
        json={"data": []},
        status=200,
    )

    ctx = FakeContext(settings=settings, library_root=tmp_path)
    ok, message = LlmCoverPlugin().test_connection(ctx)

    assert ok is False
    assert "Image models: none configured" in message


@responses.activate
def test_test_connection_without_text_base_url(tmp_path: Path) -> None:
    """test_connection fails when text base URL is not set."""
    settings: dict[str, Any] = {
        "base_url": "",
        "model": "qwen3:8b",
        "image_models": [
            {
                "label": "flux-dev",
                "api": "a1111",
                "base_url": "http://img.test:7860",
            }
        ],
    }

    ctx = FakeContext(settings=settings, library_root=tmp_path)
    ok, message = LlmCoverPlugin().test_connection(ctx)

    assert ok is False
    assert message.startswith("Text model: set the Base URL first")


@responses.activate
def test_missing_text_base_url_holds(build_epub: Any, ctx: FakeContext, view: BookView) -> None:
    """Entry run holds when text model base_url is empty."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["base_url"] = ""

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.skips) == 1
    assert (
        "on hold: set the text model's Base URL in the Cover generator settings" in ctx.skips[0][1]
    )


@responses.activate
def test_a_book_without_an_epub_fails_without_retry(build_epub: Any, ctx: FakeContext) -> None:
    """Entry run with missing EPUB reports non-retryable failure."""
    _answer_reachability_checks()
    import dataclasses

    build_epub([("Chapter 1", "https://x/1")])
    view = dataclasses.replace(
        BookView(
            book_id="b1",
            title="T",
            author="A",
            story_url=None,
            output_filename="book.epub",
            num_chapters=None,
            status=None,
            rating=None,
            cover_ref=None,
            external=ExternalLink(),
            progress=ExternalProgress(),
            custom_values={},
        ),
        output_filename=None,
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.failures == [("b1", "the book has no EPUB file", False)]


@responses.activate
def test_non_list_image_models_mean_no_models(
    build_epub: Any,
    ctx: FakeContext,
    view: BookView,
) -> None:
    """Entry run with non-list image_models falls back to no models scenario."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["image_models"] = "oops"

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.deferrals == []
    assert ctx.skips == [("b1", "no image models are configured")]


@responses.activate
def test_image_model_rows_that_are_not_objects_or_unlabelled_are_ignored(
    build_epub: Any,
    ctx: FakeContext,
    view: BookView,
) -> None:
    """Entry run filters image model rows that are non-dicts or unlabeled."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["image_models"] = [
        42,
        {"label": "  "},
        {
            "label": "flux-dev",
            "api": "a1111",
            "base_url": "http://img.test:7860",
            "model": "",
            "width": 768,
            "height": 1152,
            "prompt_prefix": "",
            "negative_prompt": "",
            "extra_params": "{}",
            "timeout_s": 60,
        },
    ]

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "test"}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    assert ctx.deferrals == [("b1", "image:flux-dev")]


@responses.activate
def test_an_image_row_without_a_usable_base_url_holds(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
) -> None:
    """Image run with empty base_url holds configuration."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("test")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    # Set base_url to empty on the first image model
    ctx.settings["image_models"][0]["base_url"] = ""
    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.skips) == 1
    assert "on hold: set the Base URL of image model flux-dev" in ctx.skips[0][1]


def test_has_cover_is_false_for_an_unreadable_epub(tmp_path: Path) -> None:
    """_has_cover returns False when EPUB file is corrupt."""
    bad_epub = tmp_path / "bad.epub"
    bad_epub.write_bytes(b"not an epub")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
    )

    assert _has_cover(view, bad_epub) is False


def test_read_text_of_nothing_is_empty() -> None:
    """_read_text handles None, non-store storage, missing files, and content."""
    # Test with None
    assert _read_text(None) == ""

    # Test with library storage (ignored)
    assert (
        _read_text(
            AssetView(
                kind="test",
                name="test",
                path="/x",
                storage="library",
                namespace="test",
            )
        )
        == ""
    )

    # Test with missing file
    assert (
        _read_text(
            AssetView(
                kind="test",
                name="test",
                path="/nonexistent/file.txt",
                storage="store",
                namespace="test",
            )
        )
        == ""
    )

    # Test with existing file
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "test.txt"
        p.write_text("  a prompt  ")
        assert (
            _read_text(
                AssetView(
                    kind="test",
                    name="test",
                    path=str(p),
                    storage="store",
                    namespace="test",
                )
            )
            == "a prompt"
        )


@responses.activate
def test_image_stage_passes_progress_and_cancellation_to_the_gateway(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Image stage passes on_progress and check_cancelled callbacks to generate_image (GEN-D20)."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("lighthouse at dusk")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    png_data = _create_test_png()
    captured_kwargs: dict[str, Any] = {}

    def fake_generate_image(api: str, base_url: str, **kwargs: Any) -> tuple[bytes, str]:
        """Fake generate_image that captures kwargs and simulates progress."""
        captured_kwargs.update(kwargs)
        # Simulate the gateway calling on_progress
        if "on_progress" in kwargs and kwargs["on_progress"]:
            kwargs["on_progress"](60.0, "step 12/20")
        return (png_data, "image/png")

    monkeypatch.setattr("llm_cover.plugin.generate_image", fake_generate_image)

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    # Verify that check_cancelled was passed and is the context's method (bound equality)
    assert captured_kwargs["check_cancelled"] == ctx.check_cancelled
    # Verify that on_progress was passed
    assert "on_progress" in captured_kwargs
    assert callable(captured_kwargs["on_progress"])
    # Verify the initial report and the progress report from the gateway
    assert (0.0, "Generating image · flux-dev", "") in ctx.reports
    assert (60.0, "Generating image · flux-dev · step 12/20", None) in ctx.reports


@responses.activate
def test_image_stage_progress_without_a_step_keeps_the_base_note(
    build_epub: Any,
    ctx: FakeContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Image stage reports base note when progress callback has no step (GEN-D20)."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])

    # Seed the prompt asset
    p = tmp_path / "prompt.txt"
    p.write_text("lighthouse at dusk")

    view = BookView(
        book_id="b1",
        title="T",
        author="A",
        story_url=None,
        output_filename="book.epub",
        num_chapters=None,
        status=None,
        rating=None,
        cover_ref=None,
        external=ExternalLink(),
        progress=ExternalProgress(),
        custom_values={},
        assets=(
            AssetView(
                kind="t2i_prompt",
                name="qwen3-8b",
                path=str(p),
                storage="store",
                namespace="llm_cover",
            ),
        ),
    )

    png_data = _create_test_png()

    def fake_generate_image(api: str, base_url: str, **kwargs: Any) -> tuple[bytes, str]:
        """Fake generate_image that simulates progress without a step."""
        if "on_progress" in kwargs and kwargs["on_progress"]:
            kwargs["on_progress"](None, None)
        return (png_data, "image/png")

    monkeypatch.setattr("llm_cover.plugin.generate_image", fake_generate_image)

    ctx.ui_context = {"stage": "image:flux-dev"}
    patches = LlmCoverPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    # Verify that the base note appears at least twice (initial + progress without step)
    count = sum(1 for percent, note, _ in ctx.reports if note == "Generating image · flux-dev")
    assert count >= 2
