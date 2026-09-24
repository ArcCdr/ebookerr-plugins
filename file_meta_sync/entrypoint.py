#!/usr/bin/env python3
"""FileMetaSync bundled plugin entrypoint — 3-way merge for cover candidates and synopsis."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
_PLUGIN_ID = "file_meta_sync"
_MAX_IMPORT_BYTES = 25 * 1024 * 1024


class PromptAbandoned(Exception):  # noqa: N818
    """The core abandoned the pending dialog; stop without writing (EXP-233, SPI 2.17)."""


def _ask_yes_no(message: str, yes: str, no: str) -> bool:
    """Emit a dialog op on stdout and read the reply from stdin.

    Args:
        message: The prompt text.
        yes: Label for the affirmative choice.
        no: Label for the negative choice.

    Returns:
        The user's answer to the yes/no prompt.

    Raises:
        PromptAbandoned: When the core abandoned the prompt or the stdio channel
            broke — fail-stopped, never fail-written.
    """
    print(  # noqa: T201
        json.dumps({"op": "dialog", "kind": "yes_no", "message": message, "yes": yes, "no": no}),
        flush=True,
    )
    line = sys.stdin.readline()
    if not line:
        raise PromptAbandoned
    try:
        obj = json.loads(line)
        abandoned = obj.get("abandoned") if isinstance(obj, dict) else True
    except json.JSONDecodeError as exc:
        raise PromptAbandoned from exc
    if abandoned:
        raise PromptAbandoned
    return bool(obj.get("answer", False))


def _report_progress(percent: float) -> None:
    """Emit a fire-and-forget progress frame on stdout (percent in 0-100).

    Args:
        percent: Progress percentage in [0, 100].
    """
    print(json.dumps({"op": "progress", "percent": percent}), flush=True)  # noqa: T201


def _notify_durable(kind: str, message: str) -> None:
    """Emit a durable notify frame on stdout so the run leaves a trace (SPI 2.16).

    Args:
        kind: Toast kind (e.g. "info", "warning").
        message: The notification text.
    """
    print(  # noqa: T201
        json.dumps({"op": "notify", "kind": kind, "message": message, "durable": True}),
        flush=True,
    )


def _get_media_type(ext: str) -> str:
    """Map extension to media type."""
    ext_lower = ext.lower()
    if ext_lower == "png":
        return "image/png"
    if ext_lower in {"jpg", "jpeg"}:
        return "image/jpeg"
    if ext_lower == "gif":
        return "image/gif"
    if ext_lower == "webp":
        return "image/webp"
    return "image/png"


def _matches_candidate_pattern(stem: str, filename: str) -> bool:
    """Check if filename matches the cover candidate pattern: stem + [.-_ ] + more text."""
    if not filename.startswith(stem):
        return False
    remainder = filename[len(stem) :]
    if not remainder:
        return False
    return remainder[0] in {".", "-", "_", " "}


def _own_candidates(assets: list[Any]) -> list[dict[str, Any]]:
    """Return this plugin's own cover_candidate rows (the core refuses writes to anyone else's)."""
    return [
        a
        for a in assets
        if a.get("kind") == "cover_candidate" and a.get("namespace", _PLUGIN_ID) == _PLUGIN_ID
    ]


def _is_bare_filename(name: str) -> bool:
    """Whether *name* is a plain file name (no directory part, no traversal)."""
    return bool(name) and "/" not in name and "\\" not in name and name not in {".", ".."}


def discover_candidates(book_dir: Path, stem: str, assets: list[Any]) -> list[Any]:
    """Import sidecar cover images beside the EPUB as stored candidates (``GEN-TR-8``).

    A new or changed sidecar matching the candidate pattern is emitted as a stored write
    carrying base64 ``data`` and ``meta`` (``source_file``, ``sha256``); one whose sha256
    still matches its stored row is skipped as unchanged. An own row (library or stored)
    whose source file has vanished from disk is emitted as a delete. Rows owned by
    another plugin's namespace are never written to or deleted. The plugin's own
    ``<stem>.cover.png`` export is never treated as a candidate.

    Args:
        book_dir: The book's directory in the library.
        stem: The EPUB filename stem, used to match candidate filenames.
        assets: The book's current asset rows, as seen on the wire.

    Returns:
        A list of asset patch writes: data imports and/or deletes.
    """
    writes: list[Any] = []

    if not book_dir.exists():
        return writes

    own = _own_candidates(assets)
    stored_by_source = {
        (a.get("meta") or {}).get("source_file"): a for a in own if a.get("storage") == "store"
    }
    found: set[str] = set()
    export_name = f"{stem}.cover.png"  # our own cover export, never a candidate

    for file_path in sorted(book_dir.iterdir()):
        filename = file_path.name
        if not file_path.is_file() or filename == export_name:
            continue
        if filename[filename.rfind(".") + 1 :].lower() not in _IMAGE_EXTENSIONS:
            continue
        if not _matches_candidate_pattern(stem, filename):
            continue

        found.add(filename)
        data = file_path.read_bytes()
        if len(data) > _MAX_IMPORT_BYTES:
            continue  # the caller logs the skip
        sha = hashlib.sha256(data).hexdigest()
        existing = stored_by_source.get(filename)
        if existing is not None and (existing.get("meta") or {}).get("sha256") == sha:
            continue  # already imported, unchanged
        ext = filename[filename.rfind(".") + 1 :]
        writes.append(
            {
                "kind": "cover_candidate",
                "name": filename,
                "media_type": _get_media_type(ext),
                "data": base64.b64encode(data).decode("ascii"),
                "meta": {"source_file": filename, "sha256": sha},
            }
        )

    for a in own:
        source = (
            (a.get("meta") or {}).get("source_file")
            if a.get("storage") == "store"
            else a.get("name")
        )
        if source not in found:
            writes.append(
                {
                    "kind": "cover_candidate",
                    "name": a["name"],
                    "delete": True,
                }
            )

    return writes


def _oversized_candidates(book_dir: Path, stem: str) -> int:
    """Count candidate-pattern sidecar images in book_dir that exceed the import size limit."""
    if not book_dir.exists():
        return 0
    export_name = f"{stem}.cover.png"
    count = 0
    for file_path in book_dir.iterdir():
        filename = file_path.name
        if not file_path.is_file() or filename == export_name:
            continue
        if filename[filename.rfind(".") + 1 :].lower() not in _IMAGE_EXTENSIONS:
            continue
        if not _matches_candidate_pattern(stem, filename):
            continue
        if file_path.stat().st_size > _MAX_IMPORT_BYTES:
            count += 1
    return count


def merge_text(
    book_id: str,
    db_value: str | None,
    file_path: Path,
    synced_hash: str | None,
    logs: list[Any],
    ask: Callable[[str, str, str], bool] | None = None,
    *,
    interactive: bool = False,
    notify: Callable[[str, str], None] | None = None,
    book_label: str = "",
) -> tuple[str | None, str | None, float | None]:
    """Perform 3-way text merge (synopsis) following ruled precedence.

    Args:
        book_id: The book identifier (used as fallback for book_label).
        db_value: The app's current value for this field (merged with overrides).
        file_path: Path to the sidecar text file.
        synced_hash: Hash of the value that was last synced; None on first sync.
        logs: List to append log entries to.
        ask: Optional callable(message, yes_label, no_label) -> bool for interactive dialogs.
        interactive: If True, use ask to prompt on conflict; otherwise apply unattended rules.
        notify: Optional callable(level, message) for durable notifications.
        book_label: User-facing label for the book; falls back to book_id.

    Returns:
        (resolved_value, resolved_hash, resolved_mtime) where resolved_value is the text to
        store in the app, resolved_hash is its SHA256, and resolved_mtime is the file mtime
        after any writes. Returns (None, None, None) if no change.

    Precedence table:
    1. Neither changed → (None, None, None).
    2. First sync (synced_hash=None, file exists, db present, hashes differ) → adopt file.
    3. File-only changed → adopt file.
    4. DB-only changed → write file from db.
    5. Both changed (synced_hash set) → if interactive & ask, ask user; else keep app.
    """
    label = book_label or book_id
    field = "synopsis"

    db_hash = hashlib.sha256((db_value or "").encode("utf-8")).hexdigest()
    file_exists = file_path.exists()
    file_text = None
    file_mtime = None
    file_hash = None

    if file_exists:
        file_text = file_path.read_text(encoding="utf-8-sig")
        file_mtime = file_path.stat().st_mtime
        file_hash = hashlib.sha256(file_text.encode("utf-8")).hexdigest()

    db_changed = (db_value is not None or synced_hash is not None) and db_hash != synced_hash
    file_changed = file_exists and file_hash != synced_hash

    # 1. Neither changed
    if not db_changed and not file_changed:
        return None, None, None

    # 2. First sync: synced_hash is None, file exists, db present, hashes differ
    if synced_hash is None and file_exists and db_value is not None and db_hash != file_hash:
        msg = (
            f'{file_path.name}: first sync — adopted the sidecar {field} into the app for "{label}"'
        )
        logs.append({"level": "info", "message": msg})
        return file_text, file_hash, file_mtime

    # 3. File-only changed
    if not db_changed and file_changed:
        return file_text, file_hash, file_mtime

    # 4. DB-only changed
    if db_changed and not file_changed:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(db_value or "", encoding="utf-8")
        file_mtime = file_path.stat().st_mtime
        return None, db_hash, file_mtime

    # 5. Both changed with tracked conflict (synced_hash is set)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_mtime_float = file_path.stat().st_mtime if file_exists else None

    if interactive and ask is not None:
        # Ask the user
        message = (
            f'The {field} of "{label}" was changed both in ebookerr and in '
            f"{file_path.name}. Which version should be kept?"
        )
        yes_label = "Keep the ebookerr version"
        no_label = f"Keep the {file_path.name} version"
        keep_db = ask(message, yes_label, no_label)

        if keep_db:
            file_path.write_text(db_value or "", encoding="utf-8")
            file_mtime_float = file_path.stat().st_mtime
            msg = f'Conflict on {file_path.name} for "{label}": kept the ebookerr version'
            logs.append({"level": "warning", "message": msg})
            return None, db_hash, file_mtime_float
        else:
            msg = f'Conflict on {file_path.name} for "{label}": kept the file version'
            logs.append({"level": "warning", "message": msg})
            return file_text, file_hash, file_mtime_float
    else:
        # Unattended: keep app value
        file_path.write_text(db_value or "", encoding="utf-8")
        file_mtime_float = file_path.stat().st_mtime
        msg = (
            f'Conflict on {file_path.name} for "{label}": kept the ebookerr '
            "version and rewrote the sidecar"
        )
        logs.append({"level": "warning", "message": msg})
        if notify is not None:
            notify_msg = (
                f'Sidecar conflict on "{label}": kept the ebookerr {field} '
                f"and rewrote {file_path.name} — the file's previous content "
                "was replaced"
            )
            notify("warning", notify_msg)
        return None, db_hash, file_mtime_float


def merge_bytes(  # noqa: C901
    book_id: str,
    db_path: str | None,
    file_path: Path,
    synced_hash: str | None,
    logs: list[Any],
    ask: Callable[[str, str, str], bool] | None = None,
    *,
    interactive: bool = False,
    notify: Callable[[str, str], None] | None = None,
    book_label: str = "",
) -> tuple[str | None, str | None, float | None]:
    """Perform 3-way bytes merge (cover) following ruled precedence.

    Args:
        book_id: The book identifier (used as fallback for book_label).
        db_path: Path to the app's current cover file (may not exist).
        file_path: Path to the sidecar cover file.
        synced_hash: Hash of the file that was last synced; None on first sync.
        logs: List to append log entries to.
        ask: Optional callable(message, yes_label, no_label) -> bool for interactive dialogs.
        interactive: If True, use ask to prompt on conflict; otherwise apply unattended rules.
        notify: Optional callable(level, message) for durable notifications.
        book_label: User-facing label for the book; falls back to book_id.

    Returns:
        (resolved_path, resolved_hash, resolved_mtime) where resolved_path is a file path
        to store as the cover reference, resolved_hash is its SHA256, and resolved_mtime is
        the file mtime after any writes. Returns (None, None, None) if no change.

    Precedence table:
    1. Neither changed → (None, None, None).
    2. First sync (synced_hash=None, file exists, db present, hashes differ) → adopt file.
    3. File-only changed → adopt file.
    4. DB-only changed → write file from db.
    5. Both changed (synced_hash set) → if interactive & ask, ask user; else keep app.
    """
    label = book_label or book_id
    field = "cover"

    db_bytes: bytes | None = None
    db_hash: str | None = None

    if db_path:
        db_file = Path(db_path)
        if db_file.exists():
            db_bytes = db_file.read_bytes()
            db_hash = hashlib.sha256(db_bytes).hexdigest()

    file_exists = file_path.exists()
    file_mtime: float | None = None
    file_hash: str | None = None

    if file_exists:
        file_bytes = file_path.read_bytes()
        file_mtime = file_path.stat().st_mtime
        file_hash = hashlib.sha256(file_bytes).hexdigest()

    db_changed = db_hash is not None and db_hash != synced_hash
    file_changed = file_exists and file_hash != synced_hash

    # 1. Neither changed
    if not db_changed and not file_changed:
        return None, None, None

    # 2. First sync: synced_hash is None, file exists, db present, hashes differ
    if synced_hash is None and file_exists and db_hash is not None and db_hash != file_hash:
        msg = (
            f'{file_path.name}: first sync — adopted the sidecar {field} into the app for "{label}"'
        )
        logs.append({"level": "info", "message": msg})
        return str(file_path), file_hash, file_mtime

    # 3. File-only changed
    if not db_changed and file_changed:
        return str(file_path), file_hash, file_mtime

    # 4. DB-only changed
    if db_changed and not file_changed:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path:
            shutil.copy(db_path, file_path)
        file_mtime = file_path.stat().st_mtime
        return None, db_hash, file_mtime

    # 5. Both changed with tracked conflict (synced_hash is set)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_mtime_float = file_path.stat().st_mtime if file_exists else None

    if interactive and ask is not None:
        # Ask the user
        message = (
            f'The {field} of "{label}" was changed both in ebookerr and in '
            f"{file_path.name}. Which version should be kept?"
        )
        yes_label = "Keep the ebookerr version"
        no_label = f"Keep the {file_path.name} version"
        keep_db = ask(message, yes_label, no_label)

        if keep_db:
            if db_path:
                shutil.copy(db_path, file_path)
            file_mtime_float = file_path.stat().st_mtime
            msg = f'Conflict on {file_path.name} for "{label}": kept the ebookerr version'
            logs.append({"level": "warning", "message": msg})
            return None, db_hash, file_mtime_float
        else:
            msg = f'Conflict on {file_path.name} for "{label}": kept the file version'
            logs.append({"level": "warning", "message": msg})
            return str(file_path), file_hash, file_mtime_float
    else:
        # Unattended: keep app value
        if db_path:
            shutil.copy(db_path, file_path)
        file_mtime_float = file_path.stat().st_mtime
        msg = (
            f'Conflict on {file_path.name} for "{label}": kept the ebookerr '
            "version and rewrote the sidecar"
        )
        logs.append({"level": "warning", "message": msg})
        if notify is not None:
            notify_msg = (
                f'Sidecar conflict on "{label}": kept the ebookerr {field} '
                f"and rewrote {file_path.name} — the file's previous content "
                "was replaced"
            )
            notify("warning", notify_msg)
        return None, db_hash, file_mtime_float


def handle_delete(book: dict[str, Any], library_root: Path, logs: list[Any] | None = None) -> None:
    """Handle BookDeleted event. Unlink sidecar files.

    A stored row's file belongs to the core; only its source sidecar is unlinked.
    """
    if logs is None:
        logs = []

    output_filename = book.get("output_filename", "")
    assets = book.get("assets", [])

    if not output_filename:
        return

    posix_path = PurePosixPath(output_filename)
    dir_part = posix_path.parent
    stem = posix_path.stem

    library_path = library_root / dir_part

    files_to_unlink = [
        library_path / f"{stem}.back_cover.txt",
        library_path / f"{stem}.synopsis.txt",
        # Still cleaned up on delete: T2I.txt if an external tool left one next to the EPUB.
        library_path / f"{stem}.T2I.txt",
        library_path / f"{stem}.cover.png",
    ]

    for asset in assets:
        if asset.get("storage", "library") == "library" and asset.get("path"):
            files_to_unlink.append(library_root / asset["path"])
        elif asset in _own_candidates(assets):
            source = (asset.get("meta") or {}).get("source_file", "")
            if _is_bare_filename(source):
                files_to_unlink.append(library_path / source)

    deleted_count = 0
    for file_path in files_to_unlink:
        if file_path.exists():
            file_path.unlink()
            deleted_count += 1

    if deleted_count > 0:
        logs.append(
            {
                "level": "info",
                "message": f"{stem}: deleted {deleted_count} sidecar file(s) on BookDeleted",
            }
        )


def handle_purge_action(
    book: dict[str, Any],
    library_root: Path,
    logs: list[Any] | None = None,
    *,
    paths: list[str] | None = None,
    assets: list[Any] | None = None,
    keep_cover: bool = False,
) -> list[Any]:
    """Handle purge_cover_candidates/purge_assets actions. Unlink candidate and source files.

    When paths is a list, unlink library_root / p for each p (skip missing). When paths
    is None, fall back to the book's own library-storage assets. Each entry in assets
    names one of this plugin's own purged stored rows; its meta.source_file sidecar is
    unlinked too. <stem>.cover.png is unlinked unless keep_cover is True. Return []
    (registry rows already gone).

    Args:
        book: The book dict from the request.
        library_root: The library root directory.
        logs: Optional list to append log entries to.
        paths: Library-relative paths of removed library rows, or None to fall back to
            the book's own library-storage assets.
        assets: This plugin's own removed stored rows, as ``{kind, name, meta}``.
        keep_cover: When True, the ``<stem>.cover.png`` export is not unlinked (set for
            a single-asset ``purge_assets`` delete, never for "Delete all candidates").

    Returns:
        Always an empty list; registry rows are already gone by this point.
    """
    if logs is None:
        logs = []

    output_filename = book.get("output_filename", "")

    if not output_filename:
        return []

    posix_path = PurePosixPath(output_filename)
    dir_part = posix_path.parent
    stem = posix_path.stem

    library_path = library_root / dir_part

    candidates_deleted = _delete_candidates(paths, book, library_root) + _delete_sources(
        assets or [], library_path
    )

    covers_deleted = 0
    if not keep_cover:
        cover_file = library_path / f"{stem}.cover.png"
        if cover_file.exists():
            cover_file.unlink()
            covers_deleted += 1

    if candidates_deleted + covers_deleted > 0:
        message = (
            f"{stem}: purged {candidates_deleted} candidate file(s) "
            f"and {covers_deleted} cover file(s)"
        )
        logs.append(
            {
                "level": "info",
                "message": message,
            }
        )

    return []


def _delete_candidates(paths: list[str] | None, book: dict[str, Any], library_root: Path) -> int:
    """Delete candidate files from *paths*, or from the book's library-storage assets.

    The library-storage fallback applies when *paths* is ``None``.
    """
    candidates_deleted = 0

    if paths is not None:
        for p in paths:
            asset_path = library_root / p
            if asset_path.exists():
                asset_path.unlink()
                candidates_deleted += 1
    else:
        assets = book.get("assets", [])
        for asset in assets:
            if (
                asset.get("kind") == "cover_candidate"
                and asset.get("storage", "library") == "library"
            ):
                asset_path = library_root / asset.get("path", "")
                if asset_path.exists():
                    asset_path.unlink()
                    candidates_deleted += 1

    return candidates_deleted


def _delete_sources(assets_meta: list[Any], library_path: Path) -> int:
    """Unlink the sidecar each purged stored row was imported from; return how many were deleted."""
    deleted = 0
    for entry in assets_meta:
        meta = entry.get("meta") if isinstance(entry, dict) else None
        source = (meta or {}).get("source_file", "")
        if not isinstance(source, str) or not _is_bare_filename(source):
            continue
        target = library_path / source
        if target.is_file():
            target.unlink()
            deleted += 1
    return deleted


def _process_synopsis(
    book_id: Any,
    synopsis: str | None,
    library_path: Path,
    stem: str,
    custom_values: dict[str, Any],
    logs: list[Any],
    ask: Callable[[str, str, str], bool] | None = None,
    *,
    interactive: bool = False,
    notify: Callable[[str, str], None] | None = None,
    book_label: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Process synopsis merge, returning (patch_fields, patch_custom_values)."""
    patch_fields: dict[str, Any] = {}
    patch_cv: dict[str, Any] = {}

    synopsis_file = library_path / f"{stem}.back_cover.txt"
    legacy_file = library_path / f"{stem}.synopsis.txt"
    if not synopsis_file.exists() and legacy_file.exists():
        legacy_file.rename(synopsis_file)
        logs.append(
            {
                "level": "info",
                "message": f"Adopted legacy sidecar {legacy_file.name} as {synopsis_file.name}",
            }
        )

    synopsis_synced_hash = custom_values.get("synopsis.synced_hash", {}).get("value")
    resolved_syn, resolved_hash, resolved_mtime = merge_text(
        book_id,
        synopsis,
        synopsis_file,
        synopsis_synced_hash,
        logs,
        ask=ask,
        interactive=interactive,
        notify=notify,
        book_label=book_label,
    )

    if resolved_syn is not None:
        patch_fields["synopsis"] = resolved_syn
        logs.append(
            {
                "level": "info",
                "message": f"{stem}: back_cover.txt adopted into DB",
            }
        )
    if resolved_hash is not None:
        patch_cv["synopsis.synced_hash"] = {
            "value": resolved_hash,
            "value_type": "string",
            "updated_at": None,
        }
        patch_cv["synopsis.synced_mtime"] = {
            "value": str(resolved_mtime),
            "value_type": "string",
            "updated_at": None,
        }
        if resolved_syn is None:
            logs.append(
                {
                    "level": "info",
                    "message": f"{stem}: back_cover.txt written",
                }
            )

    return patch_fields, patch_cv


def _process_cover(
    book_id: Any,
    cover_ref: str | None,
    library_path: Path,
    stem: str,
    custom_values: dict[str, Any],
    logs: list[Any],
    ask: Callable[[str, str, str], bool] | None = None,
    *,
    interactive: bool = False,
    notify: Callable[[str, str], None] | None = None,
    book_label: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Process cover merge, returning (patch_fields, patch_custom_values)."""
    patch_fields: dict[str, Any] = {}
    patch_cv: dict[str, Any] = {}

    cover_file = library_path / f"{stem}.cover.png"
    cover_synced_hash = custom_values.get("cover.synced_hash", {}).get("value")
    resolved_path, resolved_hash, resolved_mtime = merge_bytes(
        book_id,
        cover_ref,
        cover_file,
        cover_synced_hash,
        logs,
        ask=ask,
        interactive=interactive,
        notify=notify,
        book_label=book_label,
    )

    if resolved_path is not None:
        patch_fields["cover_ref"] = resolved_path
    if resolved_hash is not None:
        patch_cv["cover.synced_hash"] = {
            "value": resolved_hash,
            "value_type": "string",
            "updated_at": None,
        }
        patch_cv["cover.synced_mtime"] = {
            "value": str(resolved_mtime),
            "value_type": "string",
            "updated_at": None,
        }
        logs.append(
            {
                "level": "info",
                "message": f"{stem}: cover sidecar written (cover.png)",
            }
        )

    return patch_fields, patch_cv


def process_book(
    book: dict[str, Any],
    library_root: Path,
    ask: Callable[[str, str, str], bool] | None = None,
    *,
    interactive: bool = False,
) -> dict[str, Any] | None:
    """Process a single book, returning a patch dict or None."""
    book_id: str = book.get("book_id", "")
    output_filename: str = book.get("output_filename", "")
    synopsis: str | None = book.get("synopsis")
    cover_ref: str | None = book.get("cover_ref")
    assets: list[Any] = book.get("assets", [])
    custom_values: dict[str, Any] = book.get("custom_values", {})

    if not output_filename:
        return None

    posix_path = PurePosixPath(output_filename)
    dir_part = posix_path.parent
    stem = posix_path.stem
    book_label = book.get("title") or stem

    patch_fields: dict[str, Any] = {}
    patch_custom_values: dict[str, Any] = {}
    logs: list[Any] = []

    library_path = library_root / dir_part

    candidate_writes = discover_candidates(library_path, stem, assets)
    imported = 0
    removed = 0
    for w in candidate_writes:
        if "data" in w:
            imported += 1
            logs.append({"level": "debug", "message": f"{stem}: imported candidate {w['name']}"})
        elif w.get("delete"):
            removed += 1
            logs.append({"level": "debug", "message": f"{stem}: removed candidate {w['name']}"})
    if imported or removed:
        logs.append(
            {
                "level": "info",
                "message": f"{stem}: {imported} candidate(s) imported, {removed} removed",
            }
        )
    oversized = _oversized_candidates(library_path, stem)
    if oversized:
        logs.append(
            {
                "level": "warning",
                "message": f"{stem}: {oversized} candidate file(s) over 25 MiB not imported",
            }
        )

    effective_ask = ask if interactive else None

    syn_fields, syn_cv = _process_synopsis(
        book_id,
        synopsis,
        library_path,
        stem,
        custom_values,
        logs,
        effective_ask,
        interactive=interactive,
        notify=_notify_durable,
        book_label=book_label,
    )
    patch_fields.update(syn_fields)
    patch_custom_values.update(syn_cv)

    cov_fields, cov_cv = _process_cover(
        book_id,
        cover_ref,
        library_path,
        stem,
        custom_values,
        logs,
        effective_ask,
        interactive=interactive,
        notify=_notify_durable,
        book_label=book_label,
    )
    patch_fields.update(cov_fields)
    patch_custom_values.update(cov_cv)

    if not (patch_fields or patch_custom_values or candidate_writes or logs):
        return None

    changed = bool(patch_fields or patch_custom_values or candidate_writes)
    logs.append(
        {
            "level": "debug",
            "message": f"{stem}: processed (changed={changed})",
        }
    )

    result: dict[str, Any] = {
        "book_id": book_id,
        "fields": patch_fields,
        "custom_values": patch_custom_values,
    }

    if candidate_writes:
        result["assets"] = candidate_writes

    if logs:
        result["logs"] = logs

    return result


def _process_books(
    request: dict[str, Any],
    library_root: Path,
    logs: list[Any],
    ask: Callable[[str, str, str], bool] | None = None,
) -> list[Any]:
    """Process books based on event type, returning result list.

    In the default (enrich) branch, a `PromptAbandoned` raised while processing one
    book is not caught here — it propagates straight out of this function, so the
    batch aborts and every book after the abandoned one is left unprocessed
    (EXP-233).
    """
    event = request.get("event")
    req: dict[str, Any] = request.get("request", {})
    books: list[Any] = req.get("books", [])
    settings: dict[str, Any] = req.get("settings", {})
    interactive = bool(req.get("interactive", False))
    result: list[Any] = []
    total = len(books)

    if event == "BookDeleted":
        if settings.get("delete_on_book_delete", True):
            for index, book in enumerate(books):
                handle_delete(book, library_root, logs)
                _report_progress((index + 1) / total * 100.0 if total else 100.0)
        else:
            logs.append(
                {
                    "level": "info",
                    "message": "delete_on_book_delete is off: sidecar files kept",
                }
            )
    elif req.get("ui_context", {}).get("action") in {"purge_cover_candidates", "purge_assets"}:
        ui = req.get("ui_context", {})
        paths = json.loads(ui["paths"]) if ui.get("paths") else None
        purged_assets = json.loads(ui["assets"]) if ui.get("assets") else []
        keep_cover = ui.get("action") == "purge_assets"
        for index, book in enumerate(books):
            deletes = handle_purge_action(
                book, library_root, logs, paths=paths, assets=purged_assets, keep_cover=keep_cover
            )
            if deletes:
                result.append({"book_id": book.get("book_id", ""), "assets": deletes})
            _report_progress((index + 1) / total * 100.0 if total else 100.0)
    else:
        for index, book in enumerate(books):
            patch = process_book(book, library_root, ask=ask, interactive=interactive)
            if patch:
                result.append(patch)
            _report_progress((index + 1) / total * 100.0 if total else 100.0)

    return result


def main() -> None:
    """Main entrypoint."""
    logs: list[Any] = []
    try:
        request_line = sys.stdin.readline()
        if not request_line:
            raise ValueError("No input from stdin")

        request: dict[str, Any] = json.loads(request_line)
        req: dict[str, Any] = request.get("request", {})
        library_root_str: str = req.get("library_root", "")

        if not library_root_str:
            raise ValueError("No library_root in request")

        library_root = Path(library_root_str)
        result = _process_books(request, library_root, logs, ask=_ask_yes_no)

        response: dict[str, Any] = {
            "ok": True,
            "result": result,
            "logs": logs,
        }
        print(json.dumps(response))  # noqa: T201
    except PromptAbandoned:
        logs.append({"level": "warning", "message": "prompt abandoned — stopped without writing"})
        response_abandoned: dict[str, Any] = {
            "ok": True,
            "result": [],
            "logs": logs,
        }
        print(json.dumps(response_abandoned))  # noqa: T201
    except Exception as exc:
        response_err: dict[str, Any] = {
            "ok": False,
            "error": str(exc),
            "logs": logs,
        }
        print(json.dumps(response_err))  # noqa: T201
        sys.exit(0)


if __name__ == "__main__":
    main()
