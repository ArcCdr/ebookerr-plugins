"""Serve one wire request with the Komga Sync plugin (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from komga_sync.plugin import KomgaSyncPlugin

run(KomgaSyncPlugin())
