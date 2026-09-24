"""Tests for the FanFicFare-JSON -> Book field mapping (moved to service layer; ."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ebookerr_sdk.domain.ids import make_book_id
from fanficfare_source.metadata import (
    chapter_links_from_fanficfare,
    chapter_urls_from_fanficfare,
    fanficfare_book_id,
    fanficfare_json_to_book_fields,
    sanitize,
)


def _embedded_json(path: Path) -> dict[str, Any]:
    """Extract the metadata object from a FanFicFare stdout fixture."""
    text = path.read_text(encoding="utf-8")
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


def test_maps_real_the_12th_key(fanficfare_fixtures: Path) -> None:
    data = _embedded_json(fanficfare_fixtures / "the-12th-key.create.stdout")
    f = fanficfare_json_to_book_fields(data)

    assert f["title"] == "The 12th Key"
    assert f["author"] == "gabthewriter"
    assert f["story_id"] == "the-12th-key"
    assert f["story_url"] == "https://www.literotica.com/s/the-12th-key"
    assert f["section_url"] == "https://www.literotica.com/s/the-12th-key"
    assert f["author_url"].endswith("/gabthewriter/works/stories")
    assert f["category"] == "Erotic Horror"
    assert "Gang Bang" in f["erotica_tags"]
    assert f["site"] == "literotica.com"
    assert f["status"] == "Completed"
    assert f["date_published"] == datetime(2026, 5, 19, tzinfo=UTC)
    assert f["date_updated"] == datetime(2026, 5, 19, tzinfo=UTC)
    assert f["output_filename"] == "gabthewriter/The 12th Key.epub"
    # numeric coercion + empty-string-to-None
    assert f["num_chapters"] == 1
    assert f["num_words"] is None
    assert f["series"] is None
    assert f["cover_image"] is None


def test_maps_multi_chapter_fixture(fanficfare_fixtures: Path) -> None:
    data = json.loads((fanficfare_fixtures / "multi_chapter.meta.json").read_text())
    f = fanficfare_json_to_book_fields(data)
    assert f["num_chapters"] == 3
    assert f["num_words"] == 12345  # thousands separators stripped
    assert f["series"] is None


def test_fields_are_valid_book_kwargs(fanficfare_fixtures: Path) -> None:
    """Every mapped key should be compatible with a Book constructor."""
    data = _embedded_json(fanficfare_fixtures / "the-12th-key.create.stdout")
    fields = fanficfare_json_to_book_fields(data)
    assert fields["title"] == "The 12th Key"
    assert fields["author"] == "gabthewriter"
    # Verify that the structure is as expected (would be valid for Book constructor)
    assert isinstance(fields, dict)
    assert "title" in fields
    assert "author" in fields


def test_sanitize_strips_tags_and_decodes_entities() -> None:
    html = '<div class="_widget__info_1absz_119">Nightmares turned fantasy come to life.</div>'
    assert sanitize(html) == "Nightmares turned fantasy come to life."
    assert sanitize("<p>Chapter one. <b>Bold</b> bits.</p>") == "Chapter one. Bold bits."
    assert sanitize("Tom &amp; Jerry &lt;3") == "Tom & Jerry <3"


def test_sanitize_preserves_internal_whitespace_and_trims_ends() -> None:
    # internal whitespace kept (no collapse, unlike the old strip_html); ends trimmed
    assert sanitize("  a   b  ") == "a   b"


def test_sanitize_none_and_empty() -> None:
    assert sanitize(None) is None
    assert sanitize("") == ""


def test_missing_fields_become_none() -> None:
    f = fanficfare_json_to_book_fields({})
    assert f["title"] is None
    assert f["num_chapters"] is None
    assert f["num_words"] is None
    assert f["story_id"] is None
    # fanficfare_json key must NOT exist
    assert "fanficfare_json" not in f


def test_fields_have_no_fanficfare_json_key() -> None:
    """Result of fanficfare_json_to_book_fields has no 'fanficfare_json' key."""
    data = {
        "title": "Test Story",
        "storyUrl": "https://example.com/s/test",
    }
    f = fanficfare_json_to_book_fields(data)
    assert "fanficfare_json" not in f


def test_fields_map_language() -> None:
    """Input with language field maps to fields['language']; absent -> key absent."""
    # With language
    f_with_lang = fanficfare_json_to_book_fields(
        {
            "storyUrl": "https://example.com/s/test",
            "language": "German",
        }
    )
    assert f_with_lang.get("language") == "German"

    # Without language
    f_without_lang = fanficfare_json_to_book_fields(
        {
            "storyUrl": "https://example.com/s/test",
        }
    )
    assert "language" not in f_without_lang or f_without_lang.get("language") is None


def test_unparseable_int_becomes_none() -> None:
    assert fanficfare_json_to_book_fields({"numChapters": "abc"})["num_chapters"] is None


def test_json_to_fields_parses_date_published() -> None:
    """datePublished is parsed into a timezone-aware UTC datetime."""
    f = fanficfare_json_to_book_fields({"datePublished": "2026-01-01"})
    assert f["date_published"] == datetime(2026, 1, 1, tzinfo=UTC)


def test_json_to_fields_parses_date_updated() -> None:
    """dateUpdated is parsed into a timezone-aware UTC datetime."""
    f = fanficfare_json_to_book_fields({"dateUpdated": "2026-02-02"})
    assert f["date_updated"] == datetime(2026, 2, 2, tzinfo=UTC)


def test_json_to_fields_missing_dates_are_none() -> None:
    """Missing datePublished and dateUpdated fields result in None."""
    f = fanficfare_json_to_book_fields({})
    assert f["date_published"] is None
    assert f["date_updated"] is None


def test_json_to_fields_unparseable_date_is_none() -> None:
    """Unparseable date strings result in None."""
    f = fanficfare_json_to_book_fields({"dateUpdated": "sometime"})
    assert f["date_updated"] is None


def test_json_to_fields_dates_not_sanitised() -> None:
    """Date fields are parsed to datetime, not passed through sanitize."""
    f = fanficfare_json_to_book_fields({"datePublished": "2026-01-01"})
    # If the dates were sanitized (passed through _str_or_none), they would be strings.
    # Since we parse them, they should be datetime objects.
    assert isinstance(f["date_published"], datetime)
    assert f["date_published"] == datetime(2026, 1, 1, tzinfo=UTC)


class TestSanitiseAtPersistence:
    def test_description_html_stripped(self, fanficfare_fixtures: Path) -> None:
        data = _embedded_json(fanficfare_fixtures / "the-12th-key.create.stdout")
        f = fanficfare_json_to_book_fields(data)
        # Raw JSON description is HTML; must be stored as plain text.
        assert "<" not in f["description"]
        assert f["description"] == "Nightmares turned fantasy come to life."

    def test_title_and_author_sanitised(self) -> None:
        f = fanficfare_json_to_book_fields(
            {
                "title": "<b>My Story</b>",
                "author": "Author &amp; Co",
                "storyUrl": "https://example.com/s/x",
            }
        )
        assert f["title"] == "My Story"
        assert f["author"] == "Author & Co"

    def test_category_and_eroticatags_sanitised(self) -> None:
        f = fanficfare_json_to_book_fields(
            {
                "category": "<em>Erotica</em>",
                "eroticatags": "Tag &lt;1&gt;, Tag 2",
            }
        )
        assert f["category"] == "Erotica"
        assert f["erotica_tags"] == "Tag <1>, Tag 2"

    def test_status_and_site_sanitised(self) -> None:
        f = fanficfare_json_to_book_fields(
            {
                "status": "<i>In-Progress</i>",
                "site": "site &amp; more",
            }
        )
        assert f["status"] == "In-Progress"
        assert f["site"] == "site & more"

    def test_url_fields_not_sanitised(self) -> None:
        # HTML-entity in a URL must survive unchanged (it feeds make_book_id).
        url = "https://example.com/s/x?a=1&amp;b=2"
        f = fanficfare_json_to_book_fields(
            {
                "storyUrl": url,
                "sectionUrl": url,
                "authorUrl": url,
            }
        )
        assert f["story_url"] == url
        assert f["section_url"] == url
        assert f["author_url"] == url


class TestFanficfareBookId:
    """fanficfare_book_id derives the canonical id from FanFicFare's own URLs (F4)."""

    def test_uses_story_url(self) -> None:
        data = {
            "storyUrl": "https://example.com/series/1",
            "sectionUrl": "https://example.com/series/1",
        }
        assert fanficfare_book_id(data) == make_book_id("https://example.com/series/1")

    def test_falls_back_to_section_url(self) -> None:
        data = {"storyUrl": "", "sectionUrl": "https://example.com/series/2"}
        assert fanficfare_book_id(data) == make_book_id(None, "https://example.com/series/2")

    def test_none_without_urls(self) -> None:
        assert fanficfare_book_id({}) is None
        assert fanficfare_book_id({"storyUrl": "", "sectionUrl": " "}) is None


class TestChapterUrlsFromFanficfare:
    """Extract and deduplicate ordered chapter URLs from zchapters."""

    def test_chapter_urls_from_fanficfare_extracts_ordered(self) -> None:
        data = {
            "zchapters": [
                [1, {"title": "A", "url": "https://www.literotica.com/s/a"}],
                [2, {"title": "B", "url": "https://www.literotica.com/s/b"}],
            ]
        }
        assert chapter_urls_from_fanficfare(data) == [
            "https://www.literotica.com/s/a",
            "https://www.literotica.com/s/b",
        ]

    def test_chapter_urls_from_fanficfare_dedupes_preserving_order(self) -> None:
        data = {
            "zchapters": [
                [1, {"url": "a"}],
                [2, {"url": "b"}],
                [3, {"url": "a"}],
            ]
        }
        assert chapter_urls_from_fanficfare(data) == ["a", "b"]

    def test_chapter_urls_from_fanficfare_missing_key(self) -> None:
        assert chapter_urls_from_fanficfare({}) == []

    def test_chapter_urls_from_fanficfare_malformed(self) -> None:
        # zchapters is not a list
        assert chapter_urls_from_fanficfare({"zchapters": "nope"}) == []
        # Mixed malformed entries
        data = {
            "zchapters": [
                [1],  # too short
                "x",  # not a list/tuple
                [2, {"title": "no url"}],  # no url key
                [3, {"url": ""}],  # empty url
                [4, {"url": 7}],  # url is not a string
            ]
        }
        assert chapter_urls_from_fanficfare(data) == []


class TestChapterLinksFromFanficfare:
    """Extract (url, title, ordinal) triples from zchapters, deduped by URL keeping first."""

    def test_chapter_links_with_titles(self) -> None:
        """Returns (url, title, ordinal) triples, title from entry or None."""
        data = {
            "zchapters": [
                [1, {"title": "One", "url": "https://x/1"}],
                [2, {"url": "https://x/2"}],
            ]
        }
        result = chapter_links_from_fanficfare(data)
        assert result == [("https://x/1", "One", 1), ("https://x/2", None, 2)]

    def test_chapter_links_dedupes_by_url_keeping_first(self) -> None:
        """Duplicate URLs keep the first (url, title, ordinal) triple."""
        data = {
            "zchapters": [
                [1, {"title": "First", "url": "https://x/1"}],
                [2, {"title": "Second", "url": "https://x/1"}],
            ]
        }
        result = chapter_links_from_fanficfare(data)
        assert result == [("https://x/1", "First", 1)]

    def test_chapter_links_handles_missing_zchapters(self) -> None:
        """Missing zchapters returns empty list."""
        assert chapter_links_from_fanficfare({}) == []

    def test_chapter_links_handles_malformed(self) -> None:
        """Malformed zchapters gracefully returns empty or partial list."""
        data = {
            "zchapters": [
                "invalid",
                [1, {"url": "https://x/1"}],
                [2, {"title": "No URL"}],
            ]
        }
        result = chapter_links_from_fanficfare(data)
        assert result == [("https://x/1", None, 2)]
