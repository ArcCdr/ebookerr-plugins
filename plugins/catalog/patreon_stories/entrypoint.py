"""Serve one wire request with the Patreon memberships catalog (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from patreon_stories.plugin import PatreonStoriesPlugin

run(PatreonStoriesPlugin())
