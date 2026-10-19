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
    DEFAULT_LEVEL,
    LEVELS,
    RULESET_VERSION,
    NormalizeOptions,
    options_for_level,
    options_signature,
)

__all__ = [
    "DEFAULT_LEVEL",
    "LEVELS",
    "NormalizeOptions",
    "NormalizeReport",
    "RULESET_VERSION",
    "normalize_epub",
    "options_for_level",
    "options_signature",
]
