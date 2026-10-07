"""Serve one wire request with File metadata sync (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from file_meta_sync.plugin import FileMetaSyncPlugin

run(FileMetaSyncPlugin())
