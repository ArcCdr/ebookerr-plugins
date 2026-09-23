"""EPUB merge policy — the decision logic for merging multiple EPUB files.

This package owns merge policy only; generic EPUB mechanics live in
ebookerr_sdk.epub/ so every EPUB plugin shares one implementation.
"""

from __future__ import annotations

from epub_merge.merge.chapter_urls import stamp_chapter_urls
from epub_merge.merge.errors import (
    EpubMergeError,
    MergeContentError,
    MergeInputError,
    MergeStructureError,
)
from epub_merge.merge.merge import merge_epubs
from epub_merge.merge.model import MergeOptions, MergeOutcome
from epub_merge.merge.survivor import (
    SurvivorCandidate,
    candidate_from_epub,
    elect_survivor,
)

__all__ = [
    "merge_epubs",
    "stamp_chapter_urls",
    "EpubMergeError",
    "MergeInputError",
    "MergeContentError",
    "MergeStructureError",
    "MergeOptions",
    "MergeOutcome",
    "SurvivorCandidate",
    "elect_survivor",
    "candidate_from_epub",
]
