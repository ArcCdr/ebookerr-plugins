"""Serve one wire request with the Kavita Sync plugin (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from kavita_sync.plugin import KavitaSyncPlugin

run(KavitaSyncPlugin())
