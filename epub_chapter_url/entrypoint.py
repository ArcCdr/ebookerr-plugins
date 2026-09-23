"""Serve one wire request with the Chapter URL Stamp plugin (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from epub_chapter_url.plugin import EpubChapterUrlPlugin

run(EpubChapterUrlPlugin())
