"""TDD tests for the Synopsis generator run (GEN-FR-1, GEN-D7, GEN-FR-13..GEN-FR-17)."""

from __future__ import annotations

import json
import logging
import re
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
from llm_synopsis.plugin import LlmSynopsisPlugin


def _sse(*objs: dict[str, Any], done: bool = True) -> str:
    """Construct SSE stream text from JSON objects."""
    lines = [f"data: {json.dumps(o)}\n\n" for o in objs]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


def _answer_reachability_checks() -> None:
    """Answer the reachability check every generation call makes first (``GEN-D24``).

    Registers a 200 for any ``GET …/models`` and ``GET …/sdapi/v1/options``; call it first inside a
    ``@responses.activate`` test that makes a generation call.
    """
    responses.add(responses.GET, re.compile(r"^https?://[^/]+(/.*)?/models$"), json={"data": []})
    responses.add(responses.GET, re.compile(r"^https?://[^/]+/sdapi/v1/options$"), json={})


@pytest.fixture
def ctx(tmp_path: Path) -> FakeContext:
    """A FakeContext for synopsis generation tests."""
    settings: dict[str, Any] = {
        "base_url": "http://llm.test/v1",
        "model": "qwen3:8b",
        "timeout_s": 30,
        "input_words": 50,
        "max_completion_tokens": 0,
        "prompt_template": "Sum {title}: {book_text}",
    }
    return FakeContext(
        settings=settings,
        event_type=PluginEventType.BOOK_CREATED,
        library_root=tmp_path,
    )


@pytest.fixture
def view() -> BookView:
    """A minimal BookView for synopsis generation tests."""
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
def test_generates_synopsis_patch(
    build_epub: Any, ctx: FakeContext, view: BookView, caplog: pytest.LogCaptureFixture
) -> None:
    """Basic generation: returns one patch with correct fields."""
    _answer_reachability_checks()
    caplog.set_level(logging.INFO)
    build_epub([("Chapter 1", "https://x/1")])

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse(
            {"choices": [{"delta": {"content": "A quiet "}}]},
            {"choices": [{"delta": {"content": "tale."}}]},
        ),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    patch = patches[0]
    assert patch.fields == {"synopsis": "A quiet tale."}
    assert patch.custom_values["model"].value == "qwen3:8b"
    assert len(patch.assets) == 1
    assert patch.assets[0].kind == "synopsis"
    assert patch.assets[0].name == "qwen3-8b"
    assert patch.assets[0].media_type == "text/plain"
    assert patch.assets[0].data == b"A quiet tale."
    assert patch.assets[0].meta["model"] == "qwen3:8b"
    # Verify generated_at is a valid ISO-8601 timestamp (no specific text check)
    assert isinstance(patch.assets[0].meta["generated_at"], str)
    assert (
        "T" in patch.assets[0].meta["generated_at"] or "-" in patch.assets[0].meta["generated_at"]
    )  # Date separator
    assert ctx.failures == []
    assert 'Synopsis generated for "T" (book_id=b1): model=qwen3:8b, 3 word(s) in' in caplog.text


@responses.activate
def test_skips_an_overridden_synopsis(
    build_epub: Any, ctx: FakeContext, caplog: pytest.LogCaptureFixture
) -> None:
    """Skip when synopsis field is in overridden_fields."""
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
        overridden_fields=frozenset({"synopsis"}),
    )

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.skips == [("b1", "the synopsis was edited by hand")]
    assert len(responses.calls) == 0
    assert "Generation skipped" in caplog.text
    assert "the synopsis was edited by hand" in caplog.text


@responses.activate
def test_skips_when_this_model_already_produced_one(build_epub: Any, ctx: FakeContext) -> None:
    """Skip when an asset with kind=synopsis, name=model_key, namespace=plugin_id exists."""
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
                kind="synopsis",
                name="qwen3-8b",
                path="/x",
                storage="store",
                namespace="llm_synopsis",
            ),
        ),
    )

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.skips[0][0] == "b1"
    assert "qwen3:8b already exists" in ctx.skips[0][1]


@responses.activate
def test_another_models_synopsis_does_not_block(build_epub: Any, ctx: FakeContext) -> None:
    """A synopsis asset with a different model name does not block generation."""
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
                kind="synopsis",
                name="llama3-8b",
                path="/x",
                storage="store",
                namespace="llm_synopsis",
            ),
        ),
    )

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "A quiet tale."}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert len(patches) == 1
    assert patches[0].assets[0].name == "qwen3-8b"


@responses.activate
def test_another_plugins_synopsis_asset_does_not_block(build_epub: Any, ctx: FakeContext) -> None:
    """A synopsis asset with a different namespace does not block generation."""
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
                kind="synopsis",
                name="qwen3-8b",
                path="/x",
                storage="store",
                namespace="someone_else",
            ),
        ),
    )

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "A quiet tale."}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert len(patches) == 1


@responses.activate
def test_missing_model_holds_configuration(build_epub: Any, ctx: FakeContext) -> None:
    """Missing model setting holds the config breaker."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["model"] = ""
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

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.circuit.holds == [
        ("config:llm_synopsis", "set the Model in the Synopsis generator settings")
    ]


@responses.activate
def test_missing_base_url_holds_configuration(build_epub: Any, ctx: FakeContext) -> None:
    """Missing base_url setting holds the config breaker."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.settings["base_url"] = ""
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

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.skips) == 1
    assert "set the Base URL in the Synopsis generator settings" in ctx.skips[0][1]


@responses.activate
def test_unreachable_endpoint_reports_retryable_failure(build_epub: Any, ctx: FakeContext) -> None:
    """Connection error to endpoint is a retryable failure."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=requests.ConnectionError("unreachable"),
    )
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

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.failures) == 1
    assert ctx.failures[0][0] == "b1"
    assert "llm.test is not reachable" in ctx.failures[0][1]
    assert ctx.failures[0][2] is True  # retryable


@responses.activate
def test_rate_limit_holds_and_skips(build_epub: Any, ctx: FakeContext) -> None:
    """A 429 rate-limit response holds the endpoint and skips the book."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        json={"error": {"message": "rate limited"}},
        status=429,
        headers={"Retry-After": "30"},
    )
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

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.skips) == 1
    assert ctx.skips[0][0] == "b1"
    assert "on hold: rate limited" in ctx.skips[0][1]
    assert ("host:llm.test", "rate limited") in ctx.circuit.holds


@responses.activate
def test_context_too_long_is_non_retryable(build_epub: Any, ctx: FakeContext) -> None:
    """A context_length_exceeded error is a non-retryable failure."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        json={"error": {"message": "x", "code": "context_length_exceeded"}},
        status=400,
    )
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

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert len(ctx.failures) == 1
    assert "the book text is too long for the model — lower Input words" in ctx.failures[0][1]
    assert ctx.failures[0][2] is False  # not retryable


@responses.activate
def test_manual_run_also_generates(build_epub: Any, ctx: FakeContext) -> None:
    """A manual run (BOOK_UPDATED event) also generates a synopsis."""
    _answer_reachability_checks()
    build_epub([("Chapter 1", "https://x/1")])
    ctx.event_type = PluginEventType.BOOK_UPDATED
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

    responses.add(
        responses.POST,
        "http://llm.test/v1/chat/completions",
        body=_sse({"choices": [{"delta": {"content": "A quiet tale."}}]}),
        status=200,
        content_type="text/event-stream",
        stream=True,
    )

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert len(patches) == 1


@responses.activate
def test_test_connection_ok(ctx: FakeContext) -> None:
    """Test connection succeeds when endpoint responds with models."""
    responses.add(
        responses.GET,
        "http://llm.test/v1/models",
        json={"data": [{"id": "qwen3:8b"}]},
        status=200,
    )

    ok, message = LlmSynopsisPlugin().test_connection(ctx)

    assert ok is True
    assert message == "Text model: llm.test answered: 1 model(s)"


@responses.activate
def test_test_connection_unreachable(ctx: FakeContext) -> None:
    """Test connection fails when endpoint is unreachable."""
    responses.add(
        responses.GET,
        "http://llm.test/v1/models",
        body=requests.ConnectionError("Connection refused"),
    )

    ok, message = LlmSynopsisPlugin().test_connection(ctx)

    assert ok is False
    assert message == "Text model: llm.test is not reachable"


def test_test_connection_without_base_url() -> None:
    """Test connection returns early when base_url is not set."""
    ctx = FakeContext(
        settings={"base_url": ""},
        event_type=PluginEventType.BOOK_CREATED,
    )

    ok, message = LlmSynopsisPlugin().test_connection(ctx)

    assert ok is False
    assert message == "Text model: set the Base URL first"
    assert len(responses.calls) == 0


@responses.activate
def test_test_connection_sends_credential(ctx: FakeContext) -> None:
    """Test connection sends credential headers from auth_headers."""
    ctx.auth_by_host = {"llm.test": {"Authorization": "Bearer k"}}

    responses.add(
        responses.GET,
        "http://llm.test/v1/models",
        json={"data": [{"id": "qwen3:8b"}]},
        status=200,
    )

    ok, message = LlmSynopsisPlugin().test_connection(ctx)

    assert ok is True
    # Verify the request was made with the credential header
    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers.get("Authorization") == "Bearer k"


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

    patches = LlmSynopsisPlugin().enrich((view,), ctx)

    assert patches == []
    assert ctx.failures == [("b1", "the book has no EPUB file", False)]
