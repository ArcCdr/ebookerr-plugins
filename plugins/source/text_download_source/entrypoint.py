"""Serve one wire request with the Text download Source (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from text_download_source.plugin import TextDownloadSourcePlugin

run(TextDownloadSourcePlugin())
