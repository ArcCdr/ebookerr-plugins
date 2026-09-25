"""The Cover generator (``GEN-D1``): creates candidate covers with image models.

A deferred, long-running BookPlugin with a two-stage flow: a text-to-image prompt is generated
from a book's opening chapters, then one or more image-generation API calls produce candidate
covers. Runs automatically for new or imported books and on the "Generate cover candidates"
action. Never selects a candidate automatically (``GEN-D8``) and never regenerates what exists
(``GEN-D7``).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ebookerr_sdk.epub import EpubDocument, EpubError
from ebookerr_sdk.generation.client import (
    check_reachable,
    endpoint_netloc,
    generate_image,
    probe,
)
from ebookerr_sdk.generation.prompt_template import model_key, render_template
from ebookerr_sdk.generation.support import (
    endpoint_quirks_in_state,
    epub_path_for,
    generate_text,
    guarded_call,
    hold_configuration,
    is_automatic_run,
    prompt_values,
    report_generation_error,
)
from ebookerr_sdk.spi import (
    AssetView,
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

LEGACY_DEFAULT_IMAGE_PROMPT_TEMPLATE = (
    'Write one text-to-image prompt for a vertical book cover illustration for "{title}", '
    "based on the opening of the book below.\n"
    "Describe concrete visual elements — subject, setting, mood, lighting, colour palette and art "
    "style — as one comma-separated line.\n"
    "The image must contain no text, letters or typography. Answer with the prompt only, "
    "as plain text.\n\n"
    "Opening of the book:\n{book_text}"
)
"""The 2.19.1–2.19.4 default, upgraded once when unchanged (``GEN-TR-22``)."""

DEFAULT_IMAGE_PROMPT_TEMPLATE = (
    "Opening of the book:\n{book_text}\n\n"
    'Write one text-to-image prompt for a vertical book cover illustration for "{title}", '
    "based on the opening above.\n"
    "Describe concrete visual elements — subject, setting, mood, lighting, colour palette and art "
    "style — as one comma-separated line.\n"
    "The image must contain no text, letters or typography. Answer with the prompt only, "
    "as plain text."
)
"""Neutral default prompt, book text first (``GEN-D14``, ``GEN-D23``, ``GEN-TR-19``)."""

IMAGE_MODEL_FIELDS: tuple[SettingsField, ...] = (
    SettingsField(
        key="label",
        type="string",
        label="Label",
        default="",
        help="A short unique name for this model; it names the candidate, e.g. flux-dev.",
    ),
    SettingsField(
        key="api",
        type="select",
        label="API",
        default="a1111",
        options=("openai", "a1111"),
        options_strict=True,
        option_labels=("OpenAI-compatible", "AUTOMATIC1111-compatible"),
    ),
    SettingsField(
        key="base_url",
        type="url",
        label="Base URL",
        default="http://127.0.0.1:7860",
        credential=True,
    ),
    SettingsField(
        key="model",
        type="string",
        label="Model",
        default="",
        help="Leave empty on an AUTOMATIC1111-compatible server to use its loaded model.",
    ),
    SettingsField(
        key="width",
        type="int",
        label="Width",
        default=768,
    ),
    SettingsField(
        key="height",
        type="int",
        label="Height",
        default=1152,
    ),
    SettingsField(
        key="prompt_prefix",
        type="string",
        label="Prompt prefix",
        default="",
        help="Text put before the generated prompt; placeholders such as {category} work here.",
    ),
    SettingsField(
        key="negative_prompt",
        type="textarea",
        label="Negative prompt",
        default="",
    ),
    SettingsField(
        key="extra_params",
        type="textarea",
        label="Extra parameters (JSON)",
        default="{}",
        help='A JSON object merged into the request, e.g. {"steps": 8, "seed": 42}.',
    ),
    SettingsField(
        key="timeout_s",
        type="int",
        label="Timeout (s)",
        default=3600,
    ),
)
"""One image model row's fields (``GEN-FR-9``)."""

_SCHEMA = SettingsSchema(
    fields=(
        SettingsField(
            key="base_url",
            type="url",
            label="Base URL",
            default="http://127.0.0.1:11434/v1",
            group="Text model",
            credential=True,
            help="The OpenAI-compatible API base used to write the image prompt.",
        ),
        SettingsField(
            key="model",
            type="string",
            label="Model",
            default="",
            group="Text model",
        ),
        SettingsField(
            key="timeout_s",
            type="int",
            label="Timeout (s)",
            default=600,
            group="Text model",
        ),
        SettingsField(
            key="input_words",
            type="int",
            label="Input words",
            default=3000,
            group="Image prompt",
        ),
        SettingsField(
            key="max_completion_tokens",
            type="int",
            label="Maximum output tokens",
            default=0,
            group="Image prompt",
            help="0 sends no limit.",
        ),
        SettingsField(
            key="prompt_template",
            type="textarea",
            label="Prompt template",
            default=DEFAULT_IMAGE_PROMPT_TEMPLATE,
            previous_defaults=(LEGACY_DEFAULT_IMAGE_PROMPT_TEMPLATE,),
            group="Image prompt",
            help=(
                "Placeholders: {book_text} {title} {author} {category} {tags} {description} "
                "{synopsis}."
            ),
        ),
        SettingsField(
            key="image_models",
            type="record_list",
            label="Image models",
            default=[],
            group="Image models",
            fields=IMAGE_MODEL_FIELDS,
            help="Each model produces one candidate cover per book.",
            summary=("api", "width", "height"),
        ),
    ),
    groups=(
        SettingsGroup("Text model", summary="{model|No model set} · {base_url|No base URL}"),
        SettingsGroup(
            "Image prompt",
            summary=(
                "{input_words} words in · {max_completion_tokens|no output limit} output tokens"
                " · {prompt_template} prompt"
            ),
        ),
        SettingsGroup("Image models", summary="{image_models|No image models}"),
    ),
    summary="{model|No text model set} · {image_models|No image models}",
)

_MANIFEST = PluginManifest(
    id="llm_cover",
    name="Cover generator",
    version="1.1.0",
    plugin_type=PluginType.BOOK,
    settings_schema=_SCHEMA,
    headless=True,
    headed=True,
    priority=510,
    events=(PluginEventType.BOOK_CREATED, PluginEventType.BOOK_IMPORTED),
    ui_triggers=(
        UiTrigger(
            scope="book_selection_action",
            icon="imagesmode",
            label="Generate cover candidates",
            description=(
                "Create candidate covers for each selected book with the configured image models."
            ),
            min_books=1,
        ),
    ),
    custom_values=(
        CustomValueDecl(
            key="prompt_model", type="string", label="Image prompt model", filterable=True
        ),
    ),
    network=True,
    testable=True,
    deferred=True,
    long_running=True,
    default_enabled=False,
    run_timeout_s=3600,
    icon="image",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/llm_cover",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/llm_cover",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    description=(
        "Creates candidate covers for a book with image models you run or subscribe to: a "
        "language model first writes an image prompt from the opening chapters. Candidates "
        "are never set as the cover automatically."
    ),
    stage_labels=(("", "Image prompt"), ("image:", "Image")),
)


def _own_asset(view: BookView, kind: str, name: str) -> AssetView | None:
    """Return the first asset with the given kind and name in the llm_cover namespace."""
    for asset in view.assets:
        if asset.kind == kind and asset.name == name and asset.namespace == "llm_cover":
            return asset
    return None


def _valid_rows(rows: object) -> list[dict[str, Any]]:
    """Return rows whose label is non-empty, deduplicating by model_key(label)."""
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    logger = logging.getLogger("plugin.llm_cover")
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        key = model_key(label)
        if key in seen:
            logger.warning("Image model label %r repeats; the later row is ignored", label)
            continue
        seen.add(key)
        result.append(row)
    return result


def _has_cover(view: BookView, epub: Path) -> bool:
    """Return True if the book has a confirmed or embedded cover image."""
    if view.cover_ref:
        return True
    try:
        doc = EpubDocument.open(epub)
        return doc.cover_image() is not None
    except (EpubError, Exception):  # noqa: BLE001 — catch corrupt/unreadable EPUBs
        return False


def _skip(ctx: PluginContext, view: BookView, reason: str) -> None:
    """Record a skip and log it at INFO level."""
    ctx.report_skip(view.book_id, reason)
    ctx.logger.info(
        'Generation skipped for "%s" (book_id=%s) by plugin=%s: %s',
        view.title or view.book_id,
        view.book_id,
        "llm_cover",
        reason,
    )


def _read_text(asset: AssetView | None) -> str:
    """Read text from a stored asset file.

    Args:
        asset: The asset to read, or None.

    Returns:
        The file content as a string, stripped. Returns empty string if asset is None,
        or if the file does not exist, or if storage is not "store".
    """
    if asset is None:
        return ""
    if asset.storage != "store":
        return ""
    try:
        return Path(asset.path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


class LlmCoverPlugin:
    """The Cover generator (``GEN-D1``)."""

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the generator's settings schema."""
        return _SCHEMA

    def enrich(self, books: tuple[BookView, ...], ctx: PluginContext) -> list[BookPatch]:
        """Generate candidate covers for each book from the configured image models.

        Args:
            books: The books to process (one per deferred row).
            ctx: The run's context.

        Returns:
            One patch per generated cover candidate.
        """
        patches: list[BookPatch] = []
        stage = str(ctx.ui_context.get("stage", ""))
        with endpoint_quirks_in_state(ctx):
            for view in books:
                try:
                    if stage == "":
                        patch = self._entry(ctx, view)
                    elif stage.startswith("image:"):
                        patch = self._image(ctx, view, stage[len("image:") :])
                    else:
                        _skip(ctx, view, f"unknown stage {stage}")
                        continue
                except Exception as exc:  # noqa: BLE001 — mapped per outcome, unknown errors re-raised
                    report_generation_error(ctx, view, exc, plugin_id="llm_cover")
                    continue
                if patch is not None:
                    patches.append(patch)
        return patches

    def _entry(self, ctx: PluginContext, view: BookView) -> BookPatch | None:
        """Entry run: generate text prompt and queue image models."""
        base_url = str(ctx.settings.get("base_url") or "").strip()
        model = str(ctx.settings.get("model") or "").strip()

        if not base_url:
            raise hold_configuration(
                ctx,
                "llm_cover",
                self.manifest.name,
                "set the text model's Base URL in the Cover generator settings",
            )
        if not model:
            raise hold_configuration(
                ctx,
                "llm_cover",
                self.manifest.name,
                "set the text model's Model in the Cover generator settings",
            )

        epub = epub_path_for(ctx, view)
        if epub is None:
            ctx.report_failure(view.book_id, "the book has no EPUB file", retryable=False)
            return None

        if is_automatic_run(ctx) and _has_cover(view, epub):
            _skip(ctx, view, "the book already has a cover")
            return None

        rows = _valid_rows(ctx.settings.get("image_models"))
        if not rows:
            _skip(ctx, view, "no image models are configured")
            return None

        text_key = model_key(model)
        patch: BookPatch | None = None
        if _own_asset(view, "t2i_prompt", text_key) is None:
            started = time.monotonic()
            prompt = generate_text(
                ctx,
                view,
                epub,
                base_url=base_url,
                model=model,
                template=str(ctx.settings.get("prompt_template") or DEFAULT_IMAGE_PROMPT_TEMPLATE),
                input_words=int(ctx.settings.get("input_words") or 3000),
                max_completion_tokens=int(ctx.settings.get("max_completion_tokens") or 0),
                timeout_s=float(ctx.settings.get("timeout_s") or 600),
                activity="Generating image prompt",
            )
            ctx.logger.info(
                'Image prompt generated for "%s" (book_id=%s): model=%s in %.0fs',
                view.title or view.book_id,
                view.book_id,
                model,
                time.monotonic() - started,
            )
            patch = BookPatch(
                book_id=view.book_id,
                custom_values={"prompt_model": CustomValueWrite(value=model, value_type="string")},
                assets=(
                    AssetWrite(
                        kind="t2i_prompt",
                        name=text_key,
                        media_type="text/plain",
                        data=prompt.encode("utf-8"),
                        meta={
                            "model": model,
                            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                        },
                    ),
                ),
            )

        queued = present = 0
        for row in rows:
            key = model_key(str(row["label"]))
            if _own_asset(view, "cover_candidate", key) is not None:
                present += 1
                continue
            ctx.defer(view.book_id, stage=f"image:{key}")
            queued += 1
        ctx.logger.info(
            'Image generation queued for "%s" (book_id=%s): %d model(s), %d already present',
            view.title or view.book_id,
            view.book_id,
            queued,
            present,
        )
        return patch

    def _image(self, ctx: PluginContext, view: BookView, model_key_str: str) -> BookPatch | None:
        """Image run: generate cover from prompt.

        Args:
            ctx: The run's context.
            view: The book.
            model_key_str: The model_key of the image model to use.

        Returns:
            A BookPatch with the generated candidate asset, or None if generation did not occur.
        """
        # Step 1: Find the model row
        row = next(
            (
                r
                for r in _valid_rows(ctx.settings.get("image_models"))
                if model_key(str(r["label"])) == model_key_str
            ),
            None,
        )
        if row is None:
            _skip(ctx, view, f"image model {model_key_str} is no longer configured")
            return None

        # Step 2: Read the text model's prompt
        text_model = str(ctx.settings.get("model") or "").strip()
        prompt_asset = _own_asset(view, "t2i_prompt", model_key(text_model)) if text_model else None
        prompt_text = _read_text(prompt_asset)
        if not prompt_text:
            _skip(ctx, view, "the image prompt is missing")
            ctx.defer(view.book_id, stage="")
            return None

        # Step 3: Check for existing candidate
        if _own_asset(view, "cover_candidate", model_key_str) is not None:
            _skip(ctx, view, f"a candidate from {model_key_str} already exists")
            return None

        # Step 4: Validate the row
        base_url = str(row.get("base_url") or "").strip()
        if not endpoint_netloc(base_url):
            raise hold_configuration(
                ctx,
                "llm_cover",
                self.manifest.name,
                f"set the Base URL of image model {row['label']}",
            )

        try:
            extra = json.loads(str(row.get("extra_params") or "{}"))
        except ValueError:
            extra = None
        if not isinstance(extra, dict):
            raise hold_configuration(
                ctx,
                "llm_cover",
                self.manifest.name,
                f"fix Extra parameters of image model {row['label']}: enter a JSON object",
            )

        # Step 5: Build the prompt
        prefix = render_template(str(row.get("prompt_prefix") or ""), prompt_values(view, ""))
        full_prompt = f"{prefix} {prompt_text}".strip()

        # Step 6: Generate
        base_note = f"Generating image · {model_key_str}"
        ctx.report(0.0, note=base_note, detail="")

        def on_image_progress(percent: float | None, step: str | None) -> None:
            """Report the image server's own progress with its sampling step (GEN-D20)."""
            ctx.report(
                percent if percent is not None else 0.0,
                note=f"{base_note} · {step}" if step else base_note,
            )

        started = time.monotonic()
        api = str(row.get("api") or "a1111")
        headers = dict(ctx.auth_headers(base_url))
        data, media_type = guarded_call(
            ctx,
            base_url,
            lambda: generate_image(
                api,
                base_url,
                model=str(row.get("model") or ""),
                prompt=full_prompt,
                negative_prompt=str(row.get("negative_prompt") or ""),
                width=int(row.get("width") or 768),
                height=int(row.get("height") or 1152),
                extra=extra,
                timeout_s=float(row.get("timeout_s") or 3600),
                headers=headers,
                on_progress=on_image_progress,
                check_cancelled=ctx.check_cancelled,
            ),
            check=lambda: check_reachable(api, base_url, headers=headers),
        )
        ctx.logger.info(
            'Cover candidate generated for "%s" (book_id=%s): label=%s model=%s, %d bytes in %.0fs',
            view.title or view.book_id,
            view.book_id,
            row["label"],
            row.get("model") or "-",
            len(data),
            time.monotonic() - started,
        )
        return BookPatch(
            book_id=view.book_id,
            assets=(
                AssetWrite(
                    kind="cover_candidate",
                    name=model_key_str,
                    media_type=media_type,
                    data=data,
                    meta={
                        "label": str(row["label"]),
                        "api": api,
                        "model": str(row.get("model") or ""),
                        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                ),
            ),
        )

    def test_connection(self, ctx: PluginContext) -> tuple[bool, str]:
        """Check the text endpoint and every configured image endpoint (``GEN-FR-12``).

        Args:
            ctx: The test context.

        Returns:
            ``(ok, message)``: ok only when every endpoint answered; one " · "-separated part per
            endpoint (the result is shown in a toast).
        """
        with endpoint_quirks_in_state(ctx):
            lines: list[str] = []
            all_ok = True
            base_url = str(ctx.settings.get("base_url") or "").strip()
            if base_url:
                headers = dict(ctx.auth_headers(base_url))
                ok, message = probe("openai", base_url, timeout_s=30.0, headers=headers)
                lines.append(f"Text model: {message}")
                all_ok = all_ok and ok
            else:
                lines.append("Text model: set the Base URL first")
                all_ok = False
            rows = _valid_rows(ctx.settings.get("image_models"))
            if not rows:
                lines.append("Image models: none configured")
                all_ok = False
            for row in rows:
                row_url = str(row.get("base_url") or "").strip()
                headers = dict(ctx.auth_headers(row_url))
                ok, message = probe(
                    str(row.get("api") or "a1111"), row_url, timeout_s=30.0, headers=headers
                )
                lines.append(f"{row['label']}: {message}")
                all_ok = all_ok and ok
            return (all_ok, " · ".join(lines))
