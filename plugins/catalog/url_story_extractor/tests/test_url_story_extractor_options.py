"""URL Story Extractor options from the packaged base.ini (C35)."""

from __future__ import annotations

from url_story_extractor.fff_support import packaged_base_ini


def test_the_packaged_base_ini_is_the_c35_text() -> None:
    """The packaged base.ini holds the exact C35 text."""
    assert packaged_base_ini() == "[epub]\nadd_to_replace_metadata:\n oneshot=>Completed=>Completed\\,Oneshot&&numChapters=>^1$\n"
