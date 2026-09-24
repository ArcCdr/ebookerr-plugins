"""Serve one wire request with the PDF download Source (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from pdf_download_source.plugin import PdfDownloadSourcePlugin

run(PdfDownloadSourcePlugin())
