"""Serve one wire request with the URL Story Extractor (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from url_story_extractor.plugin import UrlStoryExtractorPlugin

run(UrlStoryExtractorPlugin())
