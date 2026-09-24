"""Serve one wire request with the RTF download Source (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from rtf_download_source.plugin import RtfDownloadSourcePlugin

run(RtfDownloadSourcePlugin())
