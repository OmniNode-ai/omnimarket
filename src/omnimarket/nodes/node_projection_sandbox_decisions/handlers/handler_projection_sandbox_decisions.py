"""HandlerProjectionSandboxDecisions — project sandbox invocation decisions to DB.

Consumes onex.evt.omnimarket.generated-node-invoked.v1 events and INSERTs
into the sandbox_decisions table. Append-only dedup by correlation_id
(INSERT ... ON CONFLICT (correlation_id) DO NOTHING).

Source: node_generation_consumer HandlerGeneratedExecutor emits terminal
results labelled with _runtime_backend="sandbox" on this topic for every
generated-node sandbox invocation (success or failure).

Target table schema (from migration 0001_create_sandbox_decisions.sql):
  correlation_id TEXT PRIMARY KEY
  node_name      TEXT NOT NULL
  status         TEXT NOT NULL
  runtime_backend TEXT NOT NULL
  hot_load       BOOLEAN NOT NULL
  error          TEXT
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "sandbox_decisions"
CONFLICT_KEY = "correlation_id"


class ModelSandboxDecisionEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.generated-node-invoked.v1.

    The emitter (HandlerGeneratedExecutor._terminal) produces these fields.
    extra="ignore" so future extensions to the terminal shape are forward-
    compatible without a contract bump.
    """

    correlation_id: str = Field(..., description="Unique run correlation identifier.")
    node_name: str = Field(
        ..., description="Name of the generated node that was invoked."
    )
    status: Literal["completed", "failed"] = Field(
        ..., description="Invocation outcome: completed or failed."
    )
    hot_load: bool = Field(
        default=False,
        description="Whether the invocation was a hot-load (runtime) or sandbox importlib invoke.",
    )
    runtime_backend: Literal["sandbox", "runtime"] = Field(
        default="sandbox",
        alias="_runtime_backend",
        description="Backend that ran the invocation: sandbox or runtime.",
    )
    error: str | None = Field(
        default=None,
        description="Error message when status=failed; null on success.",
    )

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_inserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionSandboxDecisions:
    """Project sandbox decision events into sandbox_decisions table.

    Append-only: dedup by correlation_id (INSERT ... ON CONFLICT DO NOTHING).
    A retry of the same sandbox invocation produces the same correlation_id
    and is silently skipped — the first outcome is the canonical record.
    """

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelSandboxDecisionEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelSandboxDecisionEvent.model_validate(input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelSandboxDecisionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """INSERT a single sandbox decision event (ON CONFLICT DO NOTHING)."""
        if db.query(TABLE, {CONFLICT_KEY: event.correlation_id}):
            return ModelProjectionResult(rows_inserted=0)

        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "node_name": event.node_name,
            "status": event.status,
            "runtime_backend": event.runtime_backend,
            "hot_load": event.hot_load,
            "error": event.error,
            "created_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_inserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelSandboxDecisionEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """INSERT a batch of sandbox decision events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_inserted
        return ModelProjectionResult(rows_inserted=count)


__all__: list[str] = [
    "HandlerProjectionSandboxDecisions",
    "ModelProjectionResult",
    "ModelSandboxDecisionEvent",
]
