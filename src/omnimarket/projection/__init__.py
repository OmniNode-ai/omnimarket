"""Projection infrastructure for Kafka->DB event projection."""

from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
)

__all__: list[str] = [
    "DatabaseAdapter",
    "InmemoryDatabaseAdapter",
]
