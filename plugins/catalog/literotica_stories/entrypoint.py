"""Serve one wire request with the Literotica search catalog (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from literotica_stories.plugin import LiteroticaStoriesPlugin

run(LiteroticaStoriesPlugin())
