"""HandlerProjectionRoutingDecision — project routing-decision events to DB.

Consumes onex.evt.omniclaude.routing-decision.v1 events and INSERTs into
the agent_routing_decisions table. Append-only; replay-safe via
INSERT ... ON CONFLICT (id) DO NOTHING (the polymorphic router mints the
UUID idempotency key).

Target table schema (from omnibase_infra migration 021, DDL owner):
  id UUID PRIMARY KEY
  correlation_id UUID
  selected_agent VARCHAR(255)
  confidence_score DECIMAL(5,4)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  request_type VARCHAR(100)
  alternatives JSONB
  routing_reason TEXT
  domain VARCHAR(255)
  metadata JSONB
  project_path TEXT
  project_name VARCHAR(255)
  claude_session_id VARCHAR(255)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "agent_routing_decisions"
CONFLICT_KEY = "id"


class ModelRoutingDecisionEvent(BaseModel):
    """Inbound event from onex.evt.omniclaude.routing-decision.v1.

    Mirrors the payload published by omniclaude's HandlerRoutingEmitter
    (ModelRoutingDecision-shaped). ``id`` is the router-minted idempotency key;
    when absent (sparse/legacy emitters) a UUID is generated so the append-only
    conflict key is always present.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str | None = Field(
        default=None, description="Router-minted UUID idempotency key."
    )
    correlation_id: str | None = Field(
        default=None, description="Trace correlation ID."
    )
    selected_agent: str = Field(default="", description="Agent selected by the router.")
    confidence_score: float | None = Field(
        default=None, description="Router confidence (0.0000-1.0000)."
    )
    created_at: str | None = Field(default=None, description="ISO 8601 timestamp.")
    request_type: str | None = Field(
        default=None, description="Type of routed request."
    )
    alternatives: list[object] | None = Field(
        default=None, description="Alternative agents considered."
    )
    routing_reason: str | None = Field(
        default=None, description="Explanation for the routing decision."
    )
    domain: str | None = Field(default=None, description="Domain classification.")
    metadata: dict[str, object] = Field(default_factory=dict)
    project_path: str | None = Field(default=None)
    project_name: str | None = Field(default=None)
    claude_session_id: str | None = Field(default=None)

    @property
    def resolved_id(self) -> str:
        resolved = (self.id or "").strip()
        return resolved or str(uuid4())


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionRoutingDecision:
    """Project routing-decision events into agent_routing_decisions table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelRoutingDecisionEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelRoutingDecisionEvent(**input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelRoutingDecisionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Append a single routing-decision event (ON CONFLICT (id) DO NOTHING)."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "id": event.resolved_id,
            "correlation_id": event.correlation_id,
            "selected_agent": event.selected_agent,
            "confidence_score": event.confidence_score,
            "created_at": event.created_at or now,
            "request_type": event.request_type,
            "alternatives": event.alternatives,
            "routing_reason": event.routing_reason,
            "domain": event.domain,
            "metadata": event.metadata,
            "project_path": event.project_path,
            "project_name": event.project_name,
            "claude_session_id": event.claude_session_id,
        }
        existing = db.query(TABLE, {CONFLICT_KEY: row["id"]})
        if existing:
            # Append-only: id already present, DO NOTHING.
            return ModelProjectionResult(rows_upserted=0)
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelRoutingDecisionEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Append a batch of routing-decision events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionRoutingDecision",
    "ModelProjectionResult",
    "ModelRoutingDecisionEvent",
]
