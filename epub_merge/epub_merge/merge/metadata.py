"""Strategies for merging bibliographic metadata across input books.

This module implements the EPUB merge's bibliographic policy: creators, contributors,
and subjects are unioned across every input book (de-duplicated in first-seen order),
dates remain the survivor's, and a language disagreement is surfaced with a warning
because a mismatch usually indicates the wrong chapters were selected. Description,
source, rights, and publisher are preserved from the survivor or overridden explicitly.

The de-duplication rules follow the EPUB bibliographic contracts:
- Creators are keyed on name alone; a non-None ``opf:file-as`` upgrades an earlier None.
- Contributors are keyed on (name, role); both are significant for rights attribution.
- Subjects are keyed on the exact string value.
- Language is selected from the override, or from distinct non-empty values; mismatches warn.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ebookerr_sdk.domain.chapter_number import extract_chapter_info

from epub_merge.merge.model import InputBook, PlannedChapter

logger = logging.getLogger(__name__)


def merge_creators(books: Sequence[InputBook]) -> tuple[tuple[str, str | None], ...]:
    """Every book's ``dc:creator``, de-duplicated by name, first occurrence wins.

    When multiple books list the same creator (by name), the first occurrence is
    retained. However, if a later occurrence has a non-None ``opf:file-as`` value
    and the first occurrence is None, the later ``file_as`` upgrades the record.
    Blank creator names are skipped.

    Args:
        books: Input books in merge order.

    Returns:
        A tuple of (name, opf:file-as) tuples, de-duplicated and in first-seen order.
    """
    seen: dict[str, str | None] = {}
    result: list[tuple[str, str | None]] = []

    for book in books:
        for name, file_as in book.creators:
            if not name:
                continue

            if name not in seen:
                seen[name] = file_as
                result.append((name, file_as))
            elif file_as is not None and seen[name] is None:
                seen[name] = file_as
                result = [(n, (file_as if n == name else fa)) for n, fa in result]

    return tuple(result)


def merge_contributors(books: Sequence[InputBook]) -> tuple[tuple[str, str], ...]:
    """Every book's ``dc:contributor``, de-duplicated by ``(name, role)``.

    Contributors are de-duplicated as a (name, role) pair. The same person
    can appear multiple times with different roles. Blank contributor names
    are skipped.

    Args:
        books: Input books in merge order.

    Returns:
        A tuple of (name, opf:role) tuples, de-duplicated and in first-seen order.
    """
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []

    for book in books:
        for name, role in book.contributors:
            if not name:
                continue

            key = (name, role)
            if key not in seen:
                seen.add(key)
                result.append(key)

    return tuple(result)


def merge_subjects(books: Sequence[InputBook]) -> tuple[str, ...]:
    """Every book's ``dc:subject``, de-duplicated, in first-seen order.

    Subjects are de-duplicated by exact string match. Blank subject strings
    are skipped.

    Args:
        books: Input books in merge order.

    Returns:
        A tuple of subject strings, de-duplicated and in first-seen order.
    """
    seen: set[str] = set()
    result: list[str] = []

    for book in books:
        for subject in book.subjects:
            if not subject:
                continue

            if subject not in seen:
                seen.add(subject)
                result.append(subject)

    return tuple(result)


def resolve_language(books: Sequence[InputBook], override: str | None) -> str:
    """Pick the output ``dc:language``; warn on a mismatch.

    When an override is provided and non-empty, it is returned verbatim with
    no logging. Otherwise, distinct non-empty language values from all books
    are collected:

    - If all books agree (0 or 1 distinct value), return it (or "en" if none).
    - If multiple distinct values exist, return the survivor's (books[0]) language
      and log a WARNING naming the conflict.

    A blank language value from a book is ignored in mismatch detection because
    it is typically not set rather than an actual disagreement.

    Args:
        books: Input books in merge order; books[0] is the survivor.
        override: Optional language code override; when non-empty, used verbatim.

    Returns:
        The resolved language code.
    """
    if override:
        return override

    distinct_langs: set[str] = set()
    for book in books:
        if book.language:
            distinct_langs.add(book.language)

    if len(distinct_langs) <= 1:
        return next(iter(distinct_langs)) if distinct_langs else "en"

    chosen = books[0].language or "en"
    lang_inputs = ", ".join(f"{book.name}={book.language}" for book in books)
    logger.warning(
        'Merge language mismatch: using "%s" from "%s" (inputs: %s)',
        chosen,
        books[0].name,
        lang_inputs,
    )
    return chosen


def build_description(books: Sequence[InputBook], override: str | None) -> str:
    r"""Return the explicit description, else one ``"<title> by <author>\n"`` line per input book.

    When ``override`` is a non-empty string, it is returned verbatim. Otherwise,
    for each book in order, emits ``f"{book.title} by {book.creators[0][0]}\n"``,
    or ``f"{book.title}\n"`` when the book has no creator. An empty book list
    yields an empty string, and a debug message is logged when auto-generated.

    Args:
        books: Input books in merge order.
        override: Optional explicit description override; when non-empty, used verbatim.

    Returns:
        The merged description string, or an empty string when no books and no override.
    """
    if override:
        return override

    lines: list[str] = []
    for book in books:
        if book.creators:
            lines.append(f"{book.title} by {book.creators[0][0]}\n")
        else:
            lines.append(f"{book.title}\n")

    text = "".join(lines)
    if not override:
        logger.debug(
            "Merge description auto-generated from %d book(s), %d character(s)",
            len(books),
            len(text),
        )

    return text


def survivor_value(books: Sequence[InputBook], field: str, override: str | None) -> str | None:
    """Return ``override`` if set, else the survivor's ``field`` when non-empty, else ``None``.

    When books is empty, returns ``override`` if set, else ``None``. Otherwise,
    reads ``getattr(books[0], field)`` (the survivor's value). If ``override``
    is a non-empty string, it is returned. If the survivor's value is a non-empty
    string, it is returned. Otherwise, ``None`` is returned (to ensure no empty
    placeholder elements are written by ``build_opf``).

    Args:
        books: Input books in merge order; books[0] is the survivor.
        field: Field name to extract from the survivor (e.g., "source", "rights").
        override: Optional override; when non-empty, used and returned verbatim.

    Returns:
        The override if set, else the survivor's field if non-empty, else None.
    """
    if not books:
        return override or None

    if override:
        return override

    survivor_val: str | None = getattr(books[0], field)
    if survivor_val:
        return survivor_val

    return None


def resolve_page_direction(books: Sequence[InputBook]) -> str | None:
    """Return the shared ``page-progression-direction``, or ``None`` when the inputs disagree.

    Collects the ``page_direction`` field from every book, treating ``""`` (empty string)
    and absent values as equivalent "unspecified" states. When the set of **non-empty**
    declared values has exactly one unique member **and** no book declares a conflicting
    value, that single value is returned. Otherwise (two different values, or no
    non-empty value at all), ``None`` is returned.

    This ensures that when an EPUB merge includes books with incompatible page directions
    (e.g., left-to-right and right-to-left), the output omits the attribute entirely.
    A READING system should never guess a direction: an absent attribute means the reader
    must infer or ask the user, which is safer than applying an incorrect default.

    Worked examples:
    - ``["rtl", "rtl"]`` → ``"rtl"`` (all agree)
    - ``["ltr", "rtl"]`` → ``None`` (conflict; omit to avoid wrong default)
    - ``["", ""]`` → ``None`` (no declaration; omit)
    - ``["rtl", ""]`` → ``"rtl"`` (one declared, others unspecified; safe to use)

    Args:
        books: Input books in merge order.

    Returns:
        The shared non-empty page direction string, or ``None`` when undecidable.
    """
    values = [book.page_direction for book in books]
    declared = {value for value in values if value}
    resolved = declared.pop() if len(declared) == 1 else None
    logger.debug("Merge page-progression-direction: %s (inputs: %s)", resolved, values)
    return resolved


def rewrite_book_title(survivor_title: str, chapters: Sequence[PlannedChapter]) -> str | None:
    """Reconstruct ``"<stem> <min>-<max>"`` from the merged chapter numbers (``MR-PARAM-2``).

    Args:
        survivor_title: The title of the survivor book (first input).
        chapters: Chapters in the merged output.

    Returns:
        A reconstructed title ``"<stem> <min>-<max>"`` when chapters carry numbers,
        or ``None`` when no chapter carries a number (the caller keeps the survivor's
        title unchanged).
    """
    numbers = [c.number for c in chapters if c.number is not None]
    if not numbers:
        logger.debug("Merge book title unchanged: no chapter in the merge set carries a number")
        return None

    info = extract_chapter_info(survivor_title)
    stem = info.book_name.strip() or survivor_title.strip()

    new_title = f"{stem} {min(numbers)}-{max(numbers)}"
    logger.info('Merge book title rewritten: "%s" -> "%s"', survivor_title, new_title)
    return new_title
