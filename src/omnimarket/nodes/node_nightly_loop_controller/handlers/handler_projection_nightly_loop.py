"""HandlerProjectionNightlyLoop — project nightly loop events to DB.

Consumes decision and iteration events, UPSERTs into
nightly_loop_decisions and nightly_loop_iterations tables.

Mirrors the HandlerProjectionDelegation pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE_DECISIONS = "nightly_loop_decisions"
TABLE_ITERATIONS = "nightly_loop_iterations"
CONFLICT_KEY_DECISIONS = "decision_id"
CONFLICT_KEY_ITERATIONS = "iteration_id"


class ModelNightlyLoopDecisionEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.nightly-loop-decision.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    decision_id: str = Field(..., description="Unique decision ID.")
    iteration_id: str = Field(..., description="Parent iteration ID.")
    correlation_id: str = Field(..., description="Root correlation ID.")
    timestamp: str | None = Field(default=None)
    action: str = Field(..., description="Action taken.")
    target: str = Field(..., description="Target of the action.")
    outcome: str = Field(..., description="Decision outcome.")
    model_used: str = Field(default="")
    cost_usd: float = Field(default=0.0, ge=0.0)
    details: str = Field(default="")


class ModelNightlyLoopIterationEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.nightly-loop-iteration-completed.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    iteration_id: str = Field(..., description="Unique iteration ID.")
    correlation_id: str = Field(..., description="Root correlation ID.")
    iteration_number: int = Field(..., ge=1)
    started_at: str | None = Field(default=None)
    completed_at: str | None = Field(default=None)
    gaps_checked: int = Field(default=0, ge=0)
    gaps_closed: int = Field(default=0, ge=0)
    decisions_made: int = Field(default=0, ge=0)
    tickets_dispatched: int = Field(default=0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    error: str | None = Field(default=None)


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default="")


class HandlerProjectionNightlyLoop:
    """Project nightly loop events into decision and iteration tables."""

    def handle(self, request: object = None) -> dict[str, object]:
        """RuntimeLocal entry point."""
        return {
            "status": "ok",
            "handler": "HandlerProjectionNightlyLoop",
            "tables": [TABLE_DECISIONS, TABLE_ITERATIONS],
            "mode": "projection",
        }

    def project_decision(
        self,
        event: ModelNightlyLoopDecisionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single decision event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "decision_id": event.decision_id,
            "iteration_id": event.iteration_id,
            "correlation_id": event.correlation_id,
            "timestamp": event.timestamp or now,
            "action": event.action,
            "target": event.target,
            "outcome": event.outcome,
            "model_used": event.model_used,
            "cost_usd": str(event.cost_usd),
            "details": event.details,
        }
        ok = db.upsert(TABLE_DECISIONS, CONFLICT_KEY_DECISIONS, row)
        return ModelProjectionResult(
            rows_upserted=1 if ok else 0, table=TABLE_DECISIONS
        )

    def project_iteration(
        self,
        event: ModelNightlyLoopIterationEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single iteration event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "iteration_id": event.iteration_id,
            "correlation_id": event.correlation_id,
            "iteration_number": event.iteration_number,
            "started_at": event.started_at or now,
            "completed_at": event.completed_at,
            "gaps_checked": event.gaps_checked,
            "gaps_closed": event.gaps_closed,
            "decisions_made": event.decisions_made,
            "tickets_dispatched": event.tickets_dispatched,
            "total_cost_usd": str(event.total_cost_usd),
            "error": event.error,
        }
        ok = db.upsert(TABLE_ITERATIONS, CONFLICT_KEY_ITERATIONS, row)
        return ModelProjectionResult(
            rows_upserted=1 if ok else 0, table=TABLE_ITERATIONS
        )


__all__: list[str] = [
    "HandlerProjectionNightlyLoop",
    "ModelNightlyLoopDecisionEvent",
    "ModelNightlyLoopIterationEvent",
    "ModelProjectionResult",
]
