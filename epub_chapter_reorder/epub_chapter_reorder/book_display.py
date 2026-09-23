"""The title the chapter reorder shows for a book.

The book record's title is what a user has actually edited (``EDIT-D15``). A title read out of
the EPUB file is a fact about the *file*, never about what the user calls the book, so it may
only ever stand in when the record has no title at all. The plugin's log lines and editor
heading resolve through :func:`display_title` so the two never diverge.
"""

from __future__ import annotations


def display_title(record_title: str | None, *, epub_title: str | None = None) -> str:
    """Return the title to show a user for one book.

    Args:
        record_title: The book record's title (``BookView.title``, already override-merged by
            the core).
        epub_title: A title read from the EPUB's own metadata (NCX ``docTitle`` or OPF
            ``dc:title``), used only when the record carries no usable title.

    Returns:
        The record's title when it is a non-blank string; otherwise the EPUB's title when
        that is non-blank; otherwise ``""``.
    """
    if record_title and record_title.strip():
        return record_title
    if epub_title and epub_title.strip():
        return epub_title
    return ""
