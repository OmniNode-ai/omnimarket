"""Projection infrastructure for Kafka->DB event projection."""

from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
    ProtocolProjectionDatabaseSync,
)
from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    default_evidence_db_path,
)

__all__: list[str] = [
    "DatabaseAdapter",
    "InmemoryDatabaseAdapter",
    "ProjectionStatus",
    "ProjectionTableConfig",
    "ProtocolProjectionDatabaseSync",
    "SqliteDatabaseAdapter",
    "build_projection_topic_map",
    "default_evidence_db_path",
]
