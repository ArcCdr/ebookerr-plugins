"""Test the part-vs-whole detection for fetched story metadata.

Tests whether the metadata received from a story URL describes that
specific story or its parent work (a multi-chapter series/collection).
"""

from url_story_extractor.plugin import _is_part


class TestRule1DifferentStoryUrl:
    """Rule 1: The site names a different canonical URL."""

    def test_a_different_story_url_is_a_part(self) -> None:
        """When meta.storyUrl differs from the requested URL, metadata is the parent's."""
        assert _is_part(
            "https://www.literotica.com/s/joan-of-snark-ch-02",
            "Joan Of Snark Ch. 02",
            {
                "storyUrl": "https://www.literotica.com/series/se/133778587",
                "title": "Joan of Snark",
                "numChapters": "17",
            },
        )

    def test_the_same_story_url_is_not_a_part(self) -> None:
        """When meta.storyUrl equals the requested URL, Rule 1 does not fire."""
        assert not _is_part(
            "https://www.literotica.com/s/angelas-stepfather-ch-03",
            "Angelas Stepfather Ch. 03",
            {
                "storyUrl": "https://www.literotica.com/s/angelas-stepfather-ch-03",
                "title": "Angela's Stepfather Ch. 03",
                "numChapters": "1",
            },
        )

    def test_story_url_comparison_ignores_www_and_scheme(self) -> None:
        """URL comparison normalises away scheme and www prefix."""
        assert not _is_part(
            "https://www.x.test/s/a",
            "A",
            {"storyUrl": "http://x.test/s/a/", "title": "A"},
        )

    def test_a_blank_story_url_is_not_rule_one(self) -> None:
        """An empty storyUrl string does not trigger Rule 1."""
        assert not _is_part(
            "https://x.test/s/a",
            "A",
            {"storyUrl": "", "title": "A"},
        )

    def test_a_non_string_story_url_is_not_rule_one(self) -> None:
        """A non-string storyUrl does not trigger Rule 1."""
        assert not _is_part(
            "https://x.test/s/a",
            "A",
            {"storyUrl": 5, "title": "A"},
        )


class TestRule2ChapterUrlUnderMultiChapterTitle:
    """Rule 2: The URL names a chapter the metadata title does not."""

    def test_a_chapter_url_under_a_multi_chapter_title_is_a_part(self) -> None:
        """URL has chapter, metadata title has none, metadata count > 1: metadata is parent's."""
        assert _is_part(
            "https://storiesonline.net/s/29762/caleb-by-pastmaster/7",
            "Caleb By Pastmaster 7",
            {
                "storyUrl": "https://storiesonline.net/s/29762/caleb-by-pastmaster/7",
                "title": "Caleb",
                "numChapters": "95",
            },
        )

    def test_a_single_chapter_work_is_not_a_part(self) -> None:
        """When numChapters is 1, Rule 2 does not fire."""
        assert not _is_part(
            "https://storiesonline.net/s/29762/caleb-by-pastmaster/7",
            "Caleb By Pastmaster 7",
            {
                "storyUrl": "https://storiesonline.net/s/29762/caleb-by-pastmaster/7",
                "title": "Caleb",
                "numChapters": "1",
            },
        )

    def test_a_missing_chapter_count_is_not_a_part(self) -> None:
        """When numChapters is absent, Rule 2 does not fire."""
        assert not _is_part(
            "https://storiesonline.net/s/29762/caleb-by-pastmaster/7",
            "Caleb By Pastmaster 7",
            {
                "storyUrl": "https://storiesonline.net/s/29762/caleb-by-pastmaster/7",
                "title": "Caleb",
            },
        )

    def test_a_chaptered_metadata_title_is_not_a_part(self) -> None:
        """When metadata title carries a chapter number, metadata is not the parent's."""
        assert not _is_part(
            "https://x.test/s/a-tale-ch-02",
            "A Tale Ch. 02",
            {
                "storyUrl": "https://x.test/s/a-tale-ch-02",
                "title": "A Tale Ch. 02",
                "numChapters": "9",
            },
        )

    def test_a_url_with_no_chapter_number_is_not_a_part(self) -> None:
        """When URL carries no chapter number, Rule 2 does not fire."""
        assert not _is_part(
            "https://x.test/s/a-tale",
            "A Tale",
            {
                "storyUrl": "https://x.test/s/a-tale",
                "title": "A Tale Collection",
                "numChapters": "9",
            },
        )

    def test_an_entity_escaped_metadata_title_is_compared_decoded(self) -> None:
        """HTML entities in the metadata title are decoded before comparison."""
        assert _is_part(
            "https://x.test/s/beauty-the-beast-ch-02",
            "Beauty The Beast Ch. 02",
            {
                "storyUrl": "https://x.test/s/beauty-the-beast-ch-02",
                "title": "Beauty &amp; the Beast",
                "numChapters": "12",
            },
        )


class TestEmptyMetadata:
    """Edge cases: empty or missing metadata."""

    def test_empty_metadata_is_not_a_part(self) -> None:
        """An empty metadata dict does not trigger either rule."""
        assert not _is_part("https://x.test/s/a", "A", {})
