"""The Synopsis generator (``GEN-D1``): writes synopses from opening chapters with a model.

A deferred, long-running BookPlugin that generates a book's synopsis from its opening words
with an OpenAI-compatible chat model. Runs automatically for new or imported books and on the
"Generate synopsis" action. Only books without a synopsis from the configured model are
processed (``GEN-D7``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from ebookerr_sdk.generation.client import endpoint_netloc, probe
from ebookerr_sdk.generation.prompt_template import model_key
from ebookerr_sdk.generation.support import (
    generate_text,
    hold_configuration,
    report_generation_error,
)
from ebookerr_sdk.spi import (
    AssetWrite,
    BookPatch,
    BookView,
    CustomValueDecl,
    CustomValueWrite,
    PluginContext,
    PluginEventType,
    PluginManifest,
    PluginType,
    SettingsField,
    SettingsGroup,
    SettingsSchema,
    UiTrigger,
)

LEGACY_DEFAULT_SYNOPSIS_TEMPLATE = (
    'Write a back-cover synopsis of at most 200 words for the book "{title}" by {author}, '
    "based on its opening below.\n"
    "Write plain text only: no title, no heading, no preamble and no notes. "
    "Do not reveal the ending.\n\n"
    "Opening of the book:\n{book_text}"
)
"""The 2.19.1–2.19.4 default, upgraded once when unchanged (``GEN-TR-22``)."""

DEFAULT_SYNOPSIS_TEMPLATE = (
    "Opening of the book:\n{book_text}\n\n"
    'Write a back-cover synopsis of at most 200 words for the book "{title}" by {author}, '
    "based on the opening above.\n"
    "Write plain text only: no title, no heading, no preamble and no notes. "
    "Do not reveal the ending."
)
"""Neutral default prompt, book text first (``GEN-D14``, ``GEN-D23``, ``GEN-TR-19``)."""

_SCHEMA = SettingsSchema(
    fields=(
        SettingsField(
            key="base_url",
            type="url",
            label="Base URL",
            default="http://127.0.0.1:11434/v1",
            required=True,
            group="Text model",
            credential=True,
            help=(
                "The OpenAI-compatible API base, ending before /chat/completions. An API "
                "key, if the server needs one, goes in Settings → Credentials as "
                '"API key (Bearer)" for this host.'
            ),
        ),
        SettingsField(
            key="model",
            type="string",
            label="Model",
            default="",
            required=True,
            group="Text model",
            help="The model name exactly as the server lists it, for example qwen3:8b.",
        ),
        SettingsField(
            key="timeout_s",
            type="int",
            label="Timeout (s)",
            default=600,
            group="Text model",
            help=(
                "How long to wait for the next piece of the answer before treating the "
                "server as unreachable."
            ),
        ),
        SettingsField(
            key="input_words",
            type="int",
            label="Input words",
            default=3000,
            group="Generation",
            help="How many words from the start of the book are sent to the model.",
        ),
        SettingsField(
            key="max_completion_tokens",
            type="int",
            label="Maximum output tokens",
            default=0,
            group="Generation",
            help="0 sends no limit. Raise it if a reasoning model stops before answering.",
        ),
        SettingsField(
            key="prompt_template",
            type="textarea",
            label="Prompt template",
            default=DEFAULT_SYNOPSIS_TEMPLATE,
            previous_defaults=(LEGACY_DEFAULT_SYNOPSIS_TEMPLATE,),
            group="Generation",
            help=(
                "Placeholders: {book_text} {title} {author} {category} {tags} {description} "
                "{synopsis}."
            ),
        ),
    ),
    groups=(
        SettingsGroup("Text model", summary="{model|No model set} · {base_url|No base URL}"),
        SettingsGroup(
            "Generation",
            summary=(
                "{input_words} words in · {max_completion_tokens|no output limit} output tokens"
                " · {prompt_template} prompt"
            ),
        ),
    ),
    summary="{model|No model set} · {prompt_template} prompt",
)

_MANIFEST = PluginManifest(
    id="llm_synopsis",
    name="Synopsis generator",
    version="1.1.0",
    plugin_type=PluginType.BOOK,
    settings_schema=_SCHEMA,
    headless=True,
    headed=True,
    priority=500,
    events=(PluginEventType.BOOK_CREATED, PluginEventType.BOOK_IMPORTED),
    ui_triggers=(
        UiTrigger(
            scope="book_selection_action",
            icon="auto_awesome",
            label="Generate synopsis",
            description="Write a synopsis for each selected book with the configured model.",
            min_books=1,
        ),
    ),
    custom_values=(
        CustomValueDecl(key="model", type="string", label="Synopsis model", filterable=True),
    ),
    network=True,
    testable=True,
    deferred=True,
    long_running=True,
    default_enabled=False,
    run_timeout_s=1800,
    icon="summarize",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/llm_synopsis",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/llm_synopsis",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    description="Writes a book's synopsis from its opening chapters with a language model you run "
    "or subscribe to. Only books without a synopsis from the configured model are processed.",
)


class LlmSynopsisPlugin:
    """The Synopsis generator (``GEN-D1``)."""

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the generator's settings schema."""
        return _SCHEMA

    def enrich(self, books: tuple[BookView, ...], ctx: PluginContext) -> list[BookPatch]:
        """Write a synopsis for each book that has none from the configured model (``GEN-FR-1``).

        A book whose synopsis the user edited is skipped, and so is a book that already holds a
        ``synopsis`` asset from this model (``GEN-D7``). Missing settings hold the plugin until its
        settings are saved (``GEN-FR-16``). Every per-book failure is reported through the context,
        so one deferred row settles as held, retried or given up.

        Args:
            books: The books to process (one per deferred row).
            ctx: The run's context.

        Returns:
            One patch per generated synopsis.
        """
        plugin_id = self.manifest.id
        settings = ctx.settings
        base_url = str(settings.get("base_url") or "").strip()
        model = str(settings.get("model") or "").strip()
        patches: list[BookPatch] = []
        for view in books:
            try:
                patch = self._generate_one(ctx, view, base_url, model, plugin_id)
            except Exception as exc:  # noqa: BLE001 — mapped per outcome, unknown errors re-raised
                report_generation_error(ctx, view, exc, plugin_id=plugin_id)
                continue
            if patch is not None:
                patches.append(patch)
        return patches

    def _generate_one(
        self, ctx: PluginContext, view: BookView, base_url: str, model: str, plugin_id: str
    ) -> BookPatch | None:
        """Generate a synopsis for one book, or skip it, or hold configuration."""
        if not endpoint_netloc(base_url):
            raise hold_configuration(
                ctx,
                plugin_id,
                self.manifest.name,
                "set the Base URL in the Synopsis generator settings",
            )
        if not model:
            raise hold_configuration(
                ctx,
                plugin_id,
                self.manifest.name,
                "set the Model in the Synopsis generator settings",
            )

        if "synopsis" in view.overridden_fields:
            _skip(ctx, view, plugin_id, "the synopsis was edited by hand")
            return None

        key = model_key(model)
        if any(
            asset.kind == "synopsis" and asset.namespace == plugin_id and asset.name == key
            for asset in view.assets
        ):
            _skip(ctx, view, plugin_id, f"a synopsis from {model} already exists")
            return None

        from ebookerr_sdk.generation.support import epub_path_for

        epub = epub_path_for(ctx, view)
        if epub is None:
            ctx.report_failure(view.book_id, "the book has no EPUB file", retryable=False)
            return None

        started = time.monotonic()
        settings = ctx.settings
        text = generate_text(
            ctx,
            view,
            epub,
            base_url=base_url,
            model=model,
            template=str(settings.get("prompt_template") or DEFAULT_SYNOPSIS_TEMPLATE),
            input_words=int(settings.get("input_words") or 3000),
            max_completion_tokens=int(settings.get("max_completion_tokens") or 0),
            timeout_s=float(settings.get("timeout_s") or 600),
            activity="Generating synopsis",
        )

        generated_at = datetime.now(UTC).isoformat(timespec="seconds")
        ctx.logger.info(
            'Synopsis generated for "%s" (book_id=%s): model=%s, %d word(s) in %.0fs',
            view.title or view.book_id,
            view.book_id,
            model,
            len(text.split()),
            time.monotonic() - started,
        )
        return BookPatch(
            book_id=view.book_id,
            fields={"synopsis": text},
            custom_values={"model": CustomValueWrite(value=model, value_type="string")},
            assets=(
                AssetWrite(
                    kind="synopsis",
                    name=key,
                    media_type="text/plain",
                    data=text.encode("utf-8"),
                    meta={"model": model, "generated_at": generated_at},
                ),
            ),
        )

    def test_connection(self, ctx: PluginContext) -> tuple[bool, str]:
        """Check the text endpoint answers and accepts the configured credential (``GEN-FR-12``).

        Args:
            ctx: The test context; reads ``base_url`` and ``timeout_s`` from ``ctx.settings``.

        Returns:
            ``(ok, message)``, one line naming the endpoint.
        """
        base_url = str(ctx.settings.get("base_url") or "").strip()
        if not base_url:
            return (False, "Text model: set the Base URL first")
        ok, message = probe(
            "openai",
            base_url,
            timeout_s=min(float(ctx.settings.get("timeout_s") or 30), 30.0),
            headers=dict(ctx.auth_headers(base_url)),
        )
        return (ok, f"Text model: {message}")


def _skip(ctx: PluginContext, view: BookView, plugin_id: str, reason: str) -> None:
    """Record a skip and log it at INFO level."""
    ctx.report_skip(view.book_id, reason)
    ctx.logger.info(
        'Generation skipped for "%s" (book_id=%s) by plugin=%s: %s',
        view.title or view.book_id,
        view.book_id,
        plugin_id,
        reason,
    )
