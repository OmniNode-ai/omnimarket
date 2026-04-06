"""HandlerProjectionRegistration — project node introspection/heartbeat to DB.

Consumes:
  - onex.evt.platform.node-introspection.v1 (full registration)
  - onex.evt.platform.node-heartbeat.v1 (health update)

UPSERTs into node_service_registry table.

Target table schema:
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  service_name TEXT UNIQUE NOT NULL
  service_url TEXT NOT NULL
  service_type TEXT (api, database, cache, queue)
  health_status TEXT DEFAULT 'unknown' (healthy, degraded, unhealthy)
  last_health_check TIMESTAMPTZ
  health_check_interval_seconds INT DEFAULT 60
  metadata JSONB DEFAULT {}
  is_active BOOLEAN DEFAULT true
  created_at TIMESTAMPTZ DEFAULT NOW()
  updated_at TIMESTAMPTZ DEFAULT NOW()
  projected_at TIMESTAMPTZ DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "node_service_registry"
CONFLICT_KEY = "service_name"


class ModelNodeIntrospectionEvent(BaseModel):
    """Inbound event from onex.evt.platform.node-introspection.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    service_name: str = Field(..., description="Unique service name.")
    service_url: str = Field(default="", description="Service endpoint URL.")
    service_type: str = Field(default="api", description="api, database, cache, queue.")
    health_status: str = Field(default="unknown")
    metadata: dict[str, object] = Field(default_factory=dict)
    is_active: bool = Field(default=True)


class ModelNodeHeartbeatEvent(BaseModel):
    """Inbound event from onex.evt.platform.node-heartbeat.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    service_name: str = Field(..., description="Unique service name.")
    health_status: str = Field(default="healthy")
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp.")


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionRegistration:
    """Project node registration and heartbeat events."""

    def project_introspection(
        self,
        event: ModelNodeIntrospectionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a full node registration from introspection."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "service_name": event.service_name,
            "service_url": event.service_url,
            "service_type": event.service_type,
            "health_status": event.health_status,
            "last_health_check": now,
            "metadata": event.metadata,
            "is_active": event.is_active,
            "updated_at": now,
            "projected_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_heartbeat(
        self,
        event: ModelNodeHeartbeatEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Update health status from a heartbeat event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "service_name": event.service_name,
            "service_url": "",
            "health_status": event.health_status,
            "last_health_check": event.timestamp or now,
            "is_active": True,
            "updated_at": now,
            "projected_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)


__all__: list[str] = [
    "HandlerProjectionRegistration",
    "ModelNodeHeartbeatEvent",
    "ModelNodeIntrospectionEvent",
    "ModelProjectionResult",
]
