"""Serve one wire request with the Chapter Reorder plugin (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from epub_chapter_reorder.plugin import EpubChapterReorderPlugin

run(EpubChapterReorderPlugin())
