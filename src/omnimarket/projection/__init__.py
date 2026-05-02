"""Projection infrastructure for Kafka->DB event projection."""

from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
    ProtocolProjectionDatabaseSync,
)

__all__: list[str] = [
    "DatabaseAdapter",
    "InmemoryDatabaseAdapter",
    "ProjectionStatus",
    "ProjectionTableConfig",
    "ProtocolProjectionDatabaseSync",
    "build_projection_topic_map",
]
