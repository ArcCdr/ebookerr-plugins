"""Serve one wire request with the EPUB download Source (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from epub_download_source.plugin import EpubDownloadSourcePlugin

run(EpubDownloadSourcePlugin())
