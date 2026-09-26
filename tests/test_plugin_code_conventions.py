"""Code conventions of the first-party plugins (moved from the core's gates in ebookerr 2.20.1)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
"""The plugins repository root."""

RESOLVE_BOOK_TITLE_ALLOWED = {"plugins/epub/epub_merge/epub_merge/merge/reader.py"}
"""Files allowed to use resolve_book_title without display_title.

reads each input EPUB's own metadata
"""


def code_files():
    """All plugin Python code files (excluding tests and __pycache__).

    Returns:
        A list of Path objects for all .py files under ROOT/plugins/
        that do not have 'tests' or '__pycache__' in their path.
    """
    result = []
    for py_file in ROOT.glob("plugins/**/*.py"):
        # Convert to relative path for checking
        try:
            relative = py_file.relative_to(ROOT)
        except ValueError:
            continue

        # Skip files in tests or __pycache__
        parts = relative.parts
        if "tests" in parts or "__pycache__" in parts:
            continue

        result.append(py_file)
    return result


def test_user_facing_titles_resolve_through_display_title():
    """User-facing titles resolve through display_title, not resolve_book_title (EDIT-D15).

    Every code file whose text contains `resolve_book_title` and whose
    POSIX relative path is not allowlisted must also contain `display_title`.
    """
    violations = []

    for py_file in code_files():
        text = py_file.read_text()
        if "resolve_book_title" not in text:
            continue

        # Get the relative path as POSIX format for allowlist checking
        try:
            relative = py_file.relative_to(ROOT)
        except ValueError:
            continue

        relative_posix = relative.as_posix()

        # If it's allowlisted, it's OK
        if relative_posix in RESOLVE_BOOK_TITLE_ALLOWED:
            continue

        # Otherwise, must have display_title
        if "display_title" not in text:
            violations.append(relative_posix)

    assert not violations, (
        "The following modules import resolve_book_title without display_title "
        "— a user-facing title must prefer the book record's title over the EPUB's own, "
        "through a display_title helper (EDIT-D15):\n" + "\n".join(f"  {v}" for v in violations)
    )


def test_the_allowlist_is_live():
    """Each allowlisted file exists and contains resolve_book_title."""
    missing_or_wrong = []

    for allowlist_path_str in RESOLVE_BOOK_TITLE_ALLOWED:
        path = ROOT / allowlist_path_str
        if not path.exists():
            missing_or_wrong.append(f"{allowlist_path_str} (does not exist)")
            continue

        text = path.read_text()
        if "resolve_book_title" not in text:
            missing_or_wrong.append(f"{allowlist_path_str} (does not contain resolve_book_title)")

    assert not missing_or_wrong, f"Allowlisted files have issues: {missing_or_wrong}"


def test_file_meta_sync_names_t2i_only_in_its_delete_list():
    """file_meta_sync entrypoint contains T2I exactly twice, never t2i (lowercase).

    T2I appears only in the files_to_unlink delete list and its comment.
    The lowercase t2i must not appear anywhere.
    """
    entrypoint_path = ROOT / "plugins/book/file_meta_sync/entrypoint.py"
    assert entrypoint_path.exists(), f"Expected file_meta_sync entrypoint at {entrypoint_path}"

    content = entrypoint_path.read_text()

    # Count occurrences of "T2I" (uppercase)
    t2i_count = content.count("T2I")
    # Count occurrences of "t2i" (lowercase)
    t2i_lower_count = content.count("t2i")

    # T2I should appear exactly twice (the files_to_unlink entry and its comment)
    assert t2i_count == 2, f"Expected T2I to appear exactly 2 times, got {t2i_count}"
    # t2i should not appear at all
    assert t2i_lower_count == 0, (
        f"Expected t2i (lowercase) to appear 0 times, got {t2i_lower_count}"
    )


def test_no_other_plugin_code_names_t2i():
    """No plugin code except llm_cover/plugin.py contains t2i or T2I.

    llm_cover/plugin.py is exempt because it reuses the t2i_prompt asset kind (GEN-FR-2).
    """
    exempt = {
        "plugins/book/llm_cover/llm_cover/plugin.py",  # GEN-FR-2 reuses the t2i_prompt asset kind
    }

    violations = []

    for py_file in code_files():
        try:
            relative = py_file.relative_to(ROOT)
        except ValueError:
            continue

        relative_posix = relative.as_posix()

        # Skip exempted files
        if relative_posix in exempt:
            continue

        # Skip the file_meta_sync/entrypoint.py (it has its own test)
        if relative_posix == "plugins/book/file_meta_sync/entrypoint.py":
            continue

        content = py_file.read_text()
        if "t2i" in content or "T2I" in content:
            violations.append(relative_posix)

    assert not violations, (
        "The following plugin files contain t2i or T2I references "
        "(only file_meta_sync/entrypoint.py and llm_cover/llm_cover/plugin.py may):\n"
        + "\n".join(f"  {v}" for v in violations)
    )
