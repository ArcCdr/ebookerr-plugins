"""Accumulator for EPUB normalization change counts.

The NormalizeCounts class uses the accumulator pattern to thread a single
mutable object through the normalization pipeline, allowing each rewriting
layer (declarations, stylesheets, chapter documents, fonts) to report how
many changes it made without passing separate integers through every
signature.

The `files_skipped_marker` counter is excluded from the `touched` property
because skipping files is the no-op outcome (nothing changed), not a change
to be reported.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

_SKIP_FIELD = "files_skipped_marker"


@dataclass(slots=True)
class NormalizeCounts:
    """Mutable accumulator for EPUB normalization change statistics.

    Attributes:
        declarations_removed: Number of CSS declarations removed.
        declarations_converted: Number of CSS declarations converted to em units.
        rules_removed: Number of CSS rules removed.
        style_attributes_removed: Number of style attributes removed from elements.
        viewport_metas_removed: Number of viewport meta tags removed.
        css_files_changed: Number of CSS files modified.
        xhtml_files_changed: Number of XHTML files modified.
        font_files_removed: Number of font files removed.
        files_skipped_marker: Number of files skipped (not counted as a change).
    """

    declarations_removed: int = 0
    declarations_converted: int = 0
    rules_removed: int = 0
    style_attributes_removed: int = 0
    viewport_metas_removed: int = 0
    css_files_changed: int = 0
    xhtml_files_changed: int = 0
    font_files_removed: int = 0
    files_skipped_marker: int = 0

    def add(self, other: NormalizeCounts) -> None:
        """Add all counters from another accumulator into this one.

        Args:
            other: Another NormalizeCounts instance to merge into this one.
        """
        for f in fields(self):
            setattr(
                self,
                f.name,
                getattr(self, f.name) + getattr(other, f.name),
            )

    @property
    def touched(self) -> bool:
        """Return whether any counter except files_skipped_marker is non-zero.

        Returns:
            True if any change counter is non-zero; False otherwise.
        """
        return any(getattr(self, f.name) for f in fields(self) if f.name != _SKIP_FIELD)
