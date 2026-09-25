"""Serve one wire request with the EPUB Validate plugin (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from epub_validate.plugin import EpubValidatePlugin

run(EpubValidatePlugin())
