"""EpubValidatePlugin — validate staged EPUBs and store findings in custom values.

An ``EpubPlugin`` (``priority=950``, the documented late band) that runs structural
validation on every EPUB ebookerr stages, collecting findings without ever modifying
the file. Runs last in the EPUB lane (``priority=950``), *after* all earlier plugins
have rewritten the file, ensuring validation sees the final bytes. Because this plugin
never writes, ``EpubEditSession``'s hash comparison detects no change and publishes
nothing — a validation run cannot re-cascade (``VAL-D1``).

Findings are stored per-book as custom values, not as ``books`` columns, allowing the
UI to render them in detail without schema migration (``VAL-D2``). The report is capped
at 32 KB and stored as a deterministic JSON array, never raising on malformed input.

The plugin accepts both native validation (built-in rule modules) and an optional
external checker (e.g., EPUBCheck); settings let the user choose and configure timeout.

One summary per run (``VAL-D10``): HEADED mode shows one alert (always, even when all
valid); HEADLESS mode shows one warning toast when any book errored or when the worst
verdict is not valid (``EXP-203`` supersedes ``VAL-D8``'s errors-only rule). Never both
surfaces in a single run.

Exception isolation (``VAL-FR-7``): a validation crash for one book never aborts the
whole pull or edit session. The item is counted as a failure and logged with a traceback,
but other items proceed. ``ctx.check_cancelled`` is *outside* the try-catch, so
cancellations propagate immediately.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from ebookerr_sdk.spi import (
    BookPatch,
    CustomValueDecl,
    CustomValueWrite,
    EpubItem,
    InvocationMode,
    PluginContext,
    PluginEventType,
    PluginManifest,
    PluginType,
    SettingsField,
    SettingsSchema,
    UiTrigger,
)
from ebookerr_sdk.validate import validate_epub
from ebookerr_sdk.validate.model import Finding

logger = logging.getLogger(__name__)

_REPORT_BYTE_LIMIT = 32 * 1024  # 32 KB (VAL-D2)

_SETTINGS_SCHEMA = SettingsSchema(
    fields=(
        SettingsField(
            key="external_checker_command",
            type="string",
            label="External EPUBCheck command",
            default="",
            help="Absolute path to an executable EPUBCheck-compatible binary. Leave empty to "
            "use the built-in checker. Nothing is downloaded or discovered automatically.",
        ),
        SettingsField(
            key="external_timeout_s",
            type="int",
            label="External checker timeout (s)",
            default=120,
            help=(
                "How long to wait for the external checker before giving up and "
                "using the built-in one."
            ),
        ),
    ),
    summary="{external_checker_command|Built-in checks only}",
)

_MANIFEST = PluginManifest(
    id="epub_validate",
    name="EPUB Validate",
    version="1.1.0",
    plugin_type=PluginType.EPUB,
    settings_schema=_SETTINGS_SCHEMA,
    headless=True,
    headed=True,
    transport="local_exec",
    priority=950,
    accepts_list=True,
    events=(PluginEventType.EPUB_CREATED, PluginEventType.EPUB_MODIFIED),
    default_enabled=True,
    run_timeout_s=600,
    icon="fact_check",
    author="ArcCdr",
    license="MIT",
    homepage="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_validate",
    source="https://github.com/ArcCdr/ebookerr-plugins/tree/main/epub_validate",
    issues="https://github.com/ArcCdr/ebookerr-plugins/issues",
    ui_triggers=(
        UiTrigger(
            scope="book_selection_action",
            icon="rule",
            label="Validate",
            description="Check the EPUB for structural errors and warnings.",
            min_books=1,
        ),
    ),
    custom_values=(
        CustomValueDecl(
            key="status",
            type="string",
            label="EPUB validation",
            filterable=True,
            aggregatable=True,
        ),
        CustomValueDecl(key="checked_at", type="datetime", label="EPUB checked"),
        CustomValueDecl(key="error_count", type="int", label="EPUB errors"),
        CustomValueDecl(key="warning_count", type="int", label="EPUB warnings"),
        CustomValueDecl(key="checker", type="string", label="EPUB checker"),
        CustomValueDecl(key="report", type="string", label="EPUB validation report"),
    ),
    description="Check every EPUB ebookerr writes for structural problems, and report what "
    "it finds without ever changing the file.",
)

_FALLBACK_COPY: Mapping[str, str] = {
    "not absolute": "external checker path is not absolute",
    "not found": "external checker not found",
    "not executable": "external checker is not executable",
    "timed out": "external checker timed out",
    "unparseable report": "external checker report could not be read",
    "oversized report": "external checker report was too large",
}

_WORST_COPY: Mapping[str, str] = {"warnings": "with warnings", "unreadable": "unreadable"}


def _fallback_suffix(reason: str | None) -> str:
    """Return the human clause naming why the built-in checker was used, or ``""`` when it wasn't.

    Args:
        reason: The fallback reason code (one of the _FALLBACK_COPY keys), or None.

    Returns:
        A string suffix for appending to result messages, or an empty string if no fallback.
    """
    if not reason:
        return ""
    return f" — {_FALLBACK_COPY.get(reason, reason)}, used the built-in checker"


def _report_json(
    findings: Sequence[Finding],
    *,
    fallback_reason: str | None = None,
    limit: int = _REPORT_BYTE_LIMIT,
) -> str:
    """Serialise findings and fallback reason to JSON, dropping findings to fit byte limit.

    Args:
        findings: The findings to serialize.
        fallback_reason: Why the external checker was not used, or None if no fallback.
        limit: The byte limit for the JSON output.

    Returns:
        A JSON object string never exceeding ``limit`` bytes, always valid JSON. The object has
        keys "findings" (array of findings) and "fallback_reason" (string or null).
    """
    # Serialize the fallback reason (human copy from _FALLBACK_COPY, or raw if unknown)
    fallback_human = (
        _FALLBACK_COPY.get(fallback_reason, fallback_reason) if fallback_reason else None
    )

    parts = [
        json.dumps(
            {"code": f.code, "severity": f.severity, "message": f.message, "location": f.location},
            ensure_ascii=False,
        )
        for f in findings
    ]
    kept: list[str] = []
    # Start with the wrapper size: {"findings": [...], "fallback_reason": null}
    # This is roughly 39 bytes (wrapper overhead excluding the findings array content)
    # We'll leave headroom for the actual final JSON serialization by using a reduced limit
    effective_limit = limit - 50  # Reserve 50 bytes for wrapper and encoding overhead
    findings_size = 2  # the enclosing "[" and "]"
    for part in parts:
        addition = len(part.encode("utf-8")) + (1 if kept else 0)  # +1 for the separating comma
        if findings_size + addition > effective_limit:
            break
        kept.append(part)
        findings_size += addition

    findings_json = "[" + ",".join(kept) + "]"

    obj = {
        "findings": json.loads(findings_json),
        "fallback_reason": fallback_human,
    }
    return json.dumps(obj, ensure_ascii=False)


def _timeout_from(settings: Mapping[str, int | str]) -> int:
    """Extract and validate the external_timeout_s setting.

    Args:
        settings: The plugin's settings dictionary.

    Returns:
        The timeout in seconds, clamped to at least 1, defaulting to 120 on coercion failure.
    """
    raw = settings.get("external_timeout_s", 120)
    try:
        value = int(raw)
        return max(1, value)
    except (TypeError, ValueError):
        return 120


def _command_from(settings: Mapping[str, int | str]) -> str | None:
    """Extract and validate the external_checker_command setting.

    Args:
        settings: The plugin's settings dictionary.

    Returns:
        The command path stripped and None-ed if blank, or None if not set.
    """
    raw = settings.get("external_checker_command", "")
    if isinstance(raw, str):
        stripped = raw.strip()
        return stripped if stripped else None
    return None


def _emit_summary(
    ctx: PluginContext,
    total: int,
    failed: int,
    worst: str,
    fallback_reason: str | None,
    worst_count: int,
    *,
    unreadable: int = 0,
) -> None:
    """Emit a summary alert or toast depending on invocation mode.

    HEADED runs alert once, always. HEADLESS runs record errors and unreadable books
    durably; a warnings-only run toasts once (transient).

    Args:
        ctx: Plugin context for alert/notify dispatch.
        total: Total number of items processed.
        failed: Number of items with status == "errors".
        worst: Status of the first non-"valid" item, or "valid" if all passed.
        fallback_reason: Why the external checker was not used, if applicable.
        worst_count: Count of items with the worst status.
        unreadable: Number of items with status == "unreadable".
    """
    summary = _summary(total, failed, worst, fallback_reason, worst_count=worst_count)
    if ctx.mode is InvocationMode.HEADED:
        ctx.alert(summary)
    elif failed or unreadable:
        ctx.notify("warning", summary, durable=True)
    elif worst != "valid":
        # Warnings only: a transient toast; the Validation card keeps the result (Q15, EXP-203).
        ctx.notify("warning", summary)


def _summary(
    total: int,
    failed: int,
    worst: str,
    fallback_reason: str | None = None,
    *,
    worst_count: int = 0,
) -> str:
    """Compute the one-line summary for a validation run.

    Args:
        total: Total number of items processed.
        failed: Number of items with status == "errors".
        worst: Status of the first non-"valid" item, or "valid" if all passed.
        fallback_reason: Why the external checker was not used, when configured but unavailable.
        worst_count: Count of items with the worst status (used for multi-book runs).

    Returns:
        A summary string for alert or toast.
    """
    suffix = _fallback_suffix(fallback_reason)
    if failed == 0 and total == 1:
        return f"EPUB validation passed: {worst}{suffix}"
    if failed == 0 and total > 1 and worst == "valid":
        return f"EPUB validation passed for all {total} book(s){suffix}"
    if failed == 0 and total > 1:
        return (
            f"EPUB validation passed for all {total} book(s) — "
            f"{worst_count} {_WORST_COPY.get(worst, worst)}{suffix}"
        )
    if failed > 0:
        return f"EPUB validation found errors in {failed} of {total} book(s){suffix}"
    return "EPUB validation had nothing to check"


class EpubValidatePlugin:
    """Validate staged EPUBs and report findings as custom values.

    Emits one summary per invocation (VAL-D10): HEADED mode alerts once, HEADLESS mode
    toasts when any book errored or when the worst verdict is not valid (EXP-203).
    Isolates validation crashes (VAL-FR-7): a single item's failure never aborts the batch.
    """

    manifest = _MANIFEST

    def settings_schema(self) -> SettingsSchema:
        """Return the settings schema the manifest declares (one schema, never a second copy).

        Returns:
            The plugin's settings schema.
        """
        return _SETTINGS_SCHEMA

    def _validate_one(
        self, item: EpubItem, ctx: PluginContext
    ) -> tuple[BookPatch | None, str, str | None]:
        """Validate a single EPUB and return its patch, status, and fallback reason.

        Runs structural validation and logs findings. On exception, logs the error and
        returns (None, "errors", None) without raising.

        Args:
            item: The EPUB item to validate.
            ctx: Plugin context for logging and settings.

        Returns:
            A tuple of (BookPatch or None, status string, fallback_reason or None).
            BookPatch is None if validation crashed; status is "errors" in that case.
        """
        report = validate_epub(
            item.epub_path,
            external_command=_command_from(ctx.settings),
            timeout_s=_timeout_from(ctx.settings),
        )

        now = datetime.now(UTC).isoformat()

        # Build the custom values write set
        patch = BookPatch(
            book_id=item.book.book_id,
            custom_values={
                "status": CustomValueWrite(
                    value=report.status, value_type="string", updated_at=now
                ),
                "checked_at": CustomValueWrite(value=now, value_type="datetime", updated_at=now),
                "error_count": CustomValueWrite(
                    value=report.error_count, value_type="int", updated_at=now
                ),
                "warning_count": CustomValueWrite(
                    value=report.warning_count, value_type="int", updated_at=now
                ),
                "checker": CustomValueWrite(
                    value=report.checker, value_type="string", updated_at=now
                ),
                "report": CustomValueWrite(
                    value=_report_json(report.findings, fallback_reason=report.fallback_reason),
                    value_type="string",
                    updated_at=now,
                ),
            },
        )

        # Log the INFO line
        ctx.logger.info(
            'EPUB validation finished for "%s" (book_id=%s): %s — '
            "%d error(s), %d warning(s) in %.2fs (checker=%s)",
            item.book.title or "(unknown title)",
            item.book.book_id,
            report.status,
            report.error_count,
            report.warning_count,
            report.duration_s,
            report.checker,
        )

        # Log each finding at DEBUG
        for finding in report.findings:
            ctx.logger.debug(
                "EPUB validation finding: book_id=%s %s [%s] %s at %s",
                item.book.book_id,
                finding.code,
                finding.severity,
                finding.message,
                finding.location,
            )

        # Log a warning if unreadable
        if report.status == "unreadable":
            ctx.logger.warning(
                'EPUB validation could not read the archive for "%s" (book_id=%s)',
                item.book.title or "(unknown title)",
                item.book.book_id,
            )

        return patch, report.status, report.fallback_reason

    def process(self, items: tuple[EpubItem, ...], ctx: PluginContext) -> list[BookPatch]:
        """Validate each staged EPUB and store findings as custom values.

        For each item: runs structural validation and collects findings, storing the
        result (status, counts, report JSON) as custom values without modifying the file.
        Never writes the EPUB, so a validation run cannot re-cascade.

        Emits one summary at the end (VAL-D10): HEADED mode alerts once, HEADLESS mode
        toasts when any book errored or when the worst verdict is not valid (EXP-203).
        On exception, the item is skipped and counted as failed; validation never aborts
        the batch (VAL-FR-7).

        Args:
            items: Staged EPUB items to validate.
            ctx: Plugin context for progress, settings, and logging.

        Returns:
            One ``BookPatch`` per successful item, carrying the validation custom values.
        """
        patches: list[BookPatch] = []
        total = len(items)
        failed = 0
        worst = "valid"
        statuses: list[str] = []
        fallback_reason: str | None = None

        for i, item in enumerate(items):
            ctx.check_cancelled()

            try:
                patch, status, fb_reason = self._validate_one(item, ctx)
            except Exception:  # noqa: BLE001 — validation must never abort a pull (VAL-FR-7)
                ctx.logger.exception(
                    'EPUB validation failed unexpectedly for "%s" (book_id=%s)',
                    item.book.title or "(unknown title)",
                    item.book.book_id,
                )
                patch, status, fb_reason = None, "errors", None

            statuses.append(status)
            if status == "errors":
                failed += 1
            if status != "valid" and worst == "valid":
                worst = status

            if patch is not None:
                patches.append(patch)
                # Collect the first non-None fallback reason from any report
                if fallback_reason is None and fb_reason is not None:
                    fallback_reason = fb_reason

            ctx.report((i + 1) / total * 100.0 if total else 100.0)

        # Log a warning if a fallback occurred
        if fallback_reason:
            logger.warning(
                "EPUB validation used the built-in checker: reason=%s "
                "(an external checker is configured)",
                fallback_reason,
            )

        # Compute worst_count for multi-book runs
        worst_count = statuses.count(worst) if worst != "valid" else 0

        # Log the run summary with verdict counts
        logger.info(
            "EPUB validation run finished: %d book(s) — %d valid, %d with warnings, "
            "%d with errors, %d unreadable",
            total,
            statuses.count("valid"),
            statuses.count("warnings"),
            statuses.count("errors"),
            statuses.count("unreadable"),
        )

        # Emit one summary per run (VAL-D10)
        unreadable_count = statuses.count("unreadable")
        _emit_summary(
            ctx, total, failed, worst, fallback_reason, worst_count, unreadable=unreadable_count
        )

        return patches
