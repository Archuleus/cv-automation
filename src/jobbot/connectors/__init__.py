"""Job sources. Each connector fetches, none of them persist."""

from jobbot.connectors.base import Connector, RawCompany, RawJob

__all__ = ["Connector", "RawCompany", "RawJob"]
