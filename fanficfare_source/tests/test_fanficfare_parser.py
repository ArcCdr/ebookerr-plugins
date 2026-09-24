"""Tests for the pure FanFicFare stdout parser (Phase 3 — riskiest contract)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fanficfare_source.parser import (
    classify_error,
    detect_was_update,
    extract_metadata_json,
)


# --------------------------- extract_metadata_json ------------------------- #
def test_extract_survives_braces_inside_json_string(fanficfare_fixtures: Path) -> None:
    """`output_css` holds literal `{`/`}` in a string -> a naive counter would fail."""
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()
    obj = extract_metadata_json(stdout)
    assert obj is not None
    assert obj["title"] == "The 12th Key"
    assert obj["author"] == "gabthewriter"
    assert obj["category"] == "Erotic Horror"
    assert "body {" in obj["output_css"]  # the embedded CSS brace survived


def test_extract_returns_none_when_no_json(fanficfare_fixtures: Path) -> None:
    stdout = (fanficfare_fixtures / "unsupported-google.stdout").read_text()
    assert extract_metadata_json(stdout) is None


def test_extract_skips_objects_missing_required_keys() -> None:
    text = 'noise {"foo": 1} more {"title":"T","author":"A","category":"C","n":2}'
    assert extract_metadata_json(text) == {"title": "T", "author": "A", "category": "C", "n": 2}


def test_extract_is_string_aware_against_naive_counter() -> None:
    text = '{"title":"T","author":"A","category":"C","css":"a } { b","ok":true}'
    obj = extract_metadata_json(text)
    assert obj is not None
    assert obj["ok"] is True
    assert obj["css"] == "a } { b"


def test_extract_skips_malformed_then_returns_valid() -> None:
    text = '{bad json} {"title":"T","author":"A","category":"C"}'
    assert extract_metadata_json(text) == {"title": "T", "author": "A", "category": "C"}


def test_extract_handles_escaped_quotes_in_string() -> None:
    text = r'{"title":"T","author":"A","category":"C","q":"he said \"hi\" }"}'
    obj = extract_metadata_json(text)
    assert obj is not None
    assert obj["q"] == 'he said "hi" }'


# ----------------------------- detect_was_update --------------------------- #
def test_detect_was_update_true_on_update_run(fanficfare_fixtures: Path) -> None:
    stdout = (fanficfare_fixtures / "the-12th-key.update.stdout").read_text()
    assert detect_was_update(stdout, "gabthewriter/The 12th Key.epub") is True


def test_detect_was_update_false_on_fresh_download(fanficfare_fixtures: Path) -> None:
    stdout = (fanficfare_fixtures / "the-12th-key.create.stdout").read_text()
    assert detect_was_update(stdout, "gabthewriter/The 12th Key.epub") is False


def test_detect_was_update_requires_matching_filename() -> None:
    assert detect_was_update("Updating other.epub, URL: x", "mine.epub") is False


def test_detect_was_update_false_when_no_filename() -> None:
    assert detect_was_update("Updating x.epub, URL: y", None) is False


# ------------------------------- classify_error ---------------------------- #
def test_classify_error_unsupported_site(fanficfare_fixtures: Path) -> None:
    stdout = (fanficfare_fixtures / "unsupported-google.stdout").read_text()
    stderr = (fanficfare_fixtures / "unsupported-google.stderr").read_text()
    msg = classify_error(stdout, stderr, 1)
    assert msg is not None
    assert "unsupported site" in msg
    assert "google.com" in msg


def test_classify_error_generic_failure(fanficfare_fixtures: Path) -> None:
    stdout = (fanficfare_fixtures / "trinity-pt-01.stdout").read_text()
    stderr = (fanficfare_fixtures / "trinity-pt-01.stderr").read_text()
    msg = classify_error(stdout, stderr, 1)
    assert msg is not None
    assert "FanFicFare failed" in msg
    assert "IndexError" in msg


def test_classify_error_none_on_success() -> None:
    assert classify_error('{"x":1}', "a benign warning", 0) is None


def test_classify_error_fallback_when_no_detail() -> None:
    msg = classify_error("", "", 1)
    assert msg is not None
    assert "unknown error" in msg


def test_classify_error_skips_trailing_blank_lines() -> None:
    assert classify_error("", "boom\n\n", 1) == "FanFicFare failed (exit 1): boom"


@pytest.mark.pins("EXP-169")
def test_classify_error_strips_the_exception_class_path() -> None:
    """FanFicFare's last stderr line often leads with a dotted exception class path."""
    result = classify_error(
        "",
        "fanficfare.exceptions.FailedToDownload: Missing Story Info block, Beta turned off?\n",
        1,
    )
    assert result == "FanFicFare failed (exit 1): Missing Story Info block, Beta turned off?"


def test_classify_error_keeps_a_plain_detail_line() -> None:
    """Plain prose without class path should not be over-stripped."""
    result = classify_error("", "could not fetch page 3\n", 1)
    assert result == "FanFicFare failed (exit 1): could not fetch page 3"
