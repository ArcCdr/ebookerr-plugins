"""Serve one wire request with the EPUB Normalize plugin (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run

from epub_normalize.plugin import EpubNormalizePlugin

run(EpubNormalizePlugin())
