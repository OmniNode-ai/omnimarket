"""HandlerProjectionDelegation — project task-delegated events to DB.

Consumes onex.evt.omniclaude.task-delegated.v1 and UPSERTs into
the delegation_events table. Dedup by correlation_id.

Target table schema (from omnidash, OMN-2284):
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  correlation_id TEXT UNIQUE NOT NULL
  session_id TEXT
  timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
  task_type TEXT NOT NULL
  delegated_to TEXT NOT NULL
  delegated_by TEXT
  quality_gate_passed BOOLEAN DEFAULT false
  quality_gates_checked JSONB
  quality_gates_failed JSONB
  delegation_latency_ms INT
  repo TEXT
  is_shadow BOOLEAN DEFAULT false
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "delegation_events"
CONFLICT_KEY = "correlation_id"


class ModelTaskDelegatedEvent(BaseModel):
    """Inbound event from onex.evt.omniclaude.task-delegated.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., description="Unique correlation ID for dedup.")
    session_id: str | None = Field(default=None)
    task_type: str = Field(..., description="Task type (e.g. code-review, refactor).")
    delegated_to: str = Field(..., description="Agent that received the task.")
    delegated_by: str | None = Field(default=None)
    quality_gate_passed: bool = Field(default=False)
    quality_gates_checked: list[str] | None = Field(default=None)
    quality_gates_failed: list[str] | None = Field(default=None)
    delegation_latency_ms: int | None = Field(default=None, ge=0)
    repo: str | None = Field(default=None)
    is_shadow: bool = Field(default=False)
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp.")


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionDelegation:
    """Project task-delegated events into delegation_events table."""

    def project(
        self,
        event: ModelTaskDelegatedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single delegation event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            "timestamp": event.timestamp or now,
            "task_type": event.task_type,
            "delegated_to": event.delegated_to,
            "delegated_by": event.delegated_by,
            "quality_gate_passed": event.quality_gate_passed,
            "quality_gates_checked": event.quality_gates_checked,
            "quality_gates_failed": event.quality_gates_failed,
            "delegation_latency_ms": event.delegation_latency_ms,
            "repo": event.repo,
            "is_shadow": event.is_shadow,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelTaskDelegatedEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of delegation events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionDelegation",
    "ModelProjectionResult",
    "ModelTaskDelegatedEvent",
]
