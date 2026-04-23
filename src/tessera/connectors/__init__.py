"""Per-client MCP configuration writers.

``tessera connect <client>`` delegates here. The registry in
:mod:`tessera.connectors.registry` maps a client id to a
:class:`~tessera.connectors.base.Connector` that knows its on-disk
path, its config format, and its merge semantics.
"""

from tessera.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorResult,
    UnknownClientError,
    UnsupportedConfigShapeError,
)
from tessera.connectors.registry import available_clients, get_connector

__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorResult",
    "UnknownClientError",
    "UnsupportedConfigShapeError",
    "available_clients",
    "get_connector",
]
