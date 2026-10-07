"""Serve one wire request with the My Literotica catalog (``ebookerr_sdk.host``)."""

from ebookerr_sdk.host import run
from my_literotica.plugin import MyLiteroticaPlugin

run(MyLiteroticaPlugin())
