"""Serve one wire request with the FanFicFare Source (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from fanficfare_source.plugin import FanFicFareSourcePlugin

run(FanFicFareSourcePlugin())
