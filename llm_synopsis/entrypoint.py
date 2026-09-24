"""Serve one wire request with the Synopsis generator (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from llm_synopsis.plugin import LlmSynopsisPlugin

run(LlmSynopsisPlugin())
