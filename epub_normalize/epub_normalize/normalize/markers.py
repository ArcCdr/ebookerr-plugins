"""Per-file idempotency markers for normalized EPUB content.

The marker lives in the file it describes, never in the package document,
precisely so it cannot survive that file being replaced or a foreign file
being added. The signature is a 12-hex digest of the ruleset version plus
the effective options from options_signature(), so changing a setting
invalidates every marker automatically.

Both stamp_css() and stamp_xhtml() are idempotent: calling them twice with
the same signature produces identical output, matching the byte-identical-no-op
contract (TR-NORM-3). This ensures the core only publishes an EPUB if its
bytes changed, preventing false EpubModified event cascades.
"""

import re

CSS_MARKER_RE: re.Pattern[str] = re.compile(r"/\*\s*ebookerr-normalized:([0-9a-f]{12})\s*\*/")
XHTML_MARKER_RE: re.Pattern[str] = re.compile(r"<!--\s*ebookerr-normalized:([0-9a-f]{12})\s*-->")

_CSS_MARKER_LINE_RE: re.Pattern[str] = re.compile(
    r"/\*\s*ebookerr-normalized:[0-9a-f]{12}\s*\*/[ \t]*\n?"
)
_LEADING_CSS_CHARSET_RE: re.Pattern[str] = re.compile(r'^@charset\s+"[A-Za-z0-9._-]+"\s*;[ \t]*\n?')
_HEAD_CLOSE_RE: re.Pattern[str] = re.compile(r"</head\s*>", re.IGNORECASE)
_HTML_CLOSE_RE: re.Pattern[str] = re.compile(r"</html\s*>", re.IGNORECASE)


def read_css_marker(text: str) -> str | None:
    """Read the 12-hex signature from the first CSS marker, if present.

    Args:
        text: CSS text to search for a marker comment.

    Returns:
        The 12-character hex signature if a valid marker is found,
        None otherwise.
    """
    match = CSS_MARKER_RE.search(text)
    return match.group(1) if match else None


def read_xhtml_marker(text: str) -> str | None:
    """Read the 12-hex signature from the first XHTML marker, if present.

    Args:
        text: XHTML text to search for a marker comment.

    Returns:
        The 12-character hex signature if a valid marker is found,
        None otherwise.
    """
    match = XHTML_MARKER_RE.search(text)
    return match.group(1) if match else None


def strip_css_marker(text: str) -> str:
    """Remove all CSS markers and their associated line breaks.

    Removes every marker occurrence together with any trailing spaces/tabs
    and a single trailing newline.

    Args:
        text: CSS text containing zero or more markers.

    Returns:
        The CSS text with all markers and their newlines removed.
    """
    return _CSS_MARKER_LINE_RE.sub("", text)


def strip_xhtml_marker(text: str) -> str:
    """Remove all XHTML markers without consuming surrounding whitespace.

    Args:
        text: XHTML text containing zero or more markers.

    Returns:
        The XHTML text with all markers removed.
    """
    return XHTML_MARKER_RE.sub("", text)


def stamp_css(text: str, signature: str) -> str:
    """Add a CSS marker as the first line, or right after a leading ``@charset`` rule.

    A stylesheet's ``@charset`` rule, when present, must remain the very first bytes of
    the file, so the marker is inserted immediately after it instead of before it. Any
    existing marker is stripped first, regardless of which of the two positions it was
    previously stamped at.

    Args:
        text: CSS text to stamp.
        signature: 12-character hex signature to embed.

    Returns:
        CSS text with the marker as the first line, or immediately after a leading
        ``@charset`` rule when the stylesheet declares one.
    """
    body = strip_css_marker(text)
    marker = f"/* ebookerr-normalized:{signature} */\n"
    charset = _LEADING_CSS_CHARSET_RE.match(body)
    if charset is None:
        return marker + body
    return body[: charset.end()] + marker + body[charset.end() :]


def stamp_xhtml(text: str, signature: str) -> str:
    """Add an XHTML marker at the first of three candidate positions.

    The insertion order is:
    1. Immediately before the first </head> (case-insensitive)
    2. Immediately before the last </html> (case-insensitive)
    3. At the end of the text (if neither tag exists)

    Any existing marker is stripped first.

    Args:
        text: XHTML text to stamp.
        signature: 12-character hex signature to embed.

    Returns:
        XHTML text with the marker inserted at the appropriate position.
    """
    marker = f"<!--ebookerr-normalized:{signature}-->"
    body = strip_xhtml_marker(text)

    # Try to insert before </head>
    head = _HEAD_CLOSE_RE.search(body)
    if head is not None:
        return body[: head.start()] + marker + body[head.start() :]

    # Try to insert before the last </html>
    closes = list(_HTML_CLOSE_RE.finditer(body))
    if closes:
        last = closes[-1]
        return body[: last.start()] + marker + body[last.start() :]

    # Fall back to appending at the end
    return body + marker
