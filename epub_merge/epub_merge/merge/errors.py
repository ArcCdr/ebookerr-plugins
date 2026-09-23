"""EPUB merge error hierarchy."""

from __future__ import annotations


class EpubMergeError(Exception):
    """Base class for every merge failure; the survivor EPUB is never modified on disk."""


class MergeInputError(EpubMergeError):
    """An input EPUB could not be read, or is missing structure the merge requires."""


class MergeContentError(EpubMergeError):
    """The merged result does not account for every input chapter."""


class MergeStructureError(EpubMergeError):
    """The merged output failed the merge's own structural self-check."""
