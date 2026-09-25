"""Serve one wire request with the DOCX download Source (``ebookerr_sdk.host``)."""

from docx_download_source.plugin import DocxDownloadSourcePlugin
from ebookerr_sdk.host import run

run(DocxDownloadSourcePlugin())
