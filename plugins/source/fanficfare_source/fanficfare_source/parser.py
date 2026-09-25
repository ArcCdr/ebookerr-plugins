r"""Pure parsing of the FanFicFare CLI's mixed text + JSON stdout/stderr.

This is the riskiest external contract (``tests/fixtures/README.md``):

* The metadata JSON is embedded among human text, and one of its string values
  (``output_css``) contains literal ``{``/``}`` — so brace-balancing must be
  **string-aware**, ignoring braces inside quoted strings (with ``\`` escapes).
* An in-place update is signalled by a line ``Updating <output_filename>, URL:``
  (FanFicFare 4.58.1 does NOT emit the spec's ``"Do update"`` string).
* An unsupported URL exits non-zero with ``UnknownSite``/``Unknown Site(...)`` on
  stderr and empty stdout.

All functions are pure (no subprocess/network/fs).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, cast

_REQUIRED_KEYS = ("author", "category", "title")
_UNKNOWN_SITE_RE = re.compile(r"Unknown Site\((?P<url>[^)]*)\)")


def _balanced_objects(text: str) -> Iterator[str]:  # noqa: C901 — string-aware state machine; clearest as one loop
    """Yield each top-level balanced ``{...}`` substring, ignoring braces in strings."""
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                yield text[start : i + 1]


def extract_metadata_json(stdout: str) -> dict[str, Any] | None:
    """Return the first embedded JSON object containing author+category+title."""
    for candidate in _balanced_objects(stdout):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if all(key in obj for key in _REQUIRED_KEYS):
            return cast("dict[str, Any]", obj)
    return None


def detect_was_update(stdout: str, output_filename: str | None) -> bool:
    """True when FanFicFare updated an existing file (``Updating <file>`` line)."""
    if not output_filename:
        return False
    prefix = f"Updating {output_filename}"
    return any(line.startswith(prefix) for line in stdout.splitlines())


def _last_nonempty_line(text: str) -> str:
    """Return the last non-blank, stripped line of ``text``, or ``""`` if none."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def classify_error(stdout: str, stderr: str, returncode: int) -> str | None:
    """Return a human error message for a failed run, or None if it looks ok.

    Strips leading dotted-class paths (e.g., "fanficfare.exceptions.FailedToDownload: …")
    from stderr detail, keeping only the actionable message after the colon (EXP-169).
    """
    match = _UNKNOWN_SITE_RE.search(stderr)
    if match:
        return f"unsupported site: FanFicFare does not support {match.group('url')}"
    if returncode != 0:
        detail = _last_nonempty_line(stderr) or _last_nonempty_line(stdout) or "unknown error"
        # EXP-169: FanFicFare's last stderr line often leads with the raising class's dotted
        # path ("fanficfare.exceptions.FailedToDownload: …"); the human half after the colon
        # is the actionable part and the only part a user-facing message may carry.
        detail = re.sub(r"^(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*:\s*", "", detail)
        return f"FanFicFare failed (exit {returncode}): {detail}"
    return None
