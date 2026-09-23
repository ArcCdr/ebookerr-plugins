"""Policy for the EPUB Normalizer plugin.

Generic EPUB mechanics stay in ebookerr_sdk.epub; normalization-specific
policy lives in this package.
"""

from __future__ import annotations

from .normalize import (
    NormalizeReport,
    normalize_epub,
)
from .options import (
    RULESET_VERSION,
    NormalizeOptions,
    options_signature,
)

__all__ = [
    "NormalizeOptions",
    "NormalizeReport",
    "RULESET_VERSION",
    "normalize_epub",
    "options_signature",
]
