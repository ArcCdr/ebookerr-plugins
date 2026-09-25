"""Serve one wire request with the EPUB Merge plugin (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from epub_merge.plugin import EpubMergePlugin

run(EpubMergePlugin())
