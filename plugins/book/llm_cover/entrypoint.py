"""Serve one wire request with the Cover generator (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from llm_cover.plugin import LlmCoverPlugin

run(LlmCoverPlugin())
