"""Serve one wire request with the FanFicFare Source (``ebookerr_sdk.host``)."""

from fanficfare_source.plugin import FanFicFareSourcePlugin

if __name__ == "__main__":
    from ebookerr_sdk.host import wire_host

    wire_host(FanFicFareSourcePlugin)
