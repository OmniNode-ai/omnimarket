"""HandlerProjectionSessionOutcome — project session-outcome events to DB.

Consumes onex.evt.omniclaude.session-outcome.v1 events and UPSERTs into
the session_outcomes table. Replay-safe via UPSERT on session_id.

Target table schema (from omnidash migration 0021):
  session_id TEXT PRIMARY KEY
  outcome TEXT NOT NULL
  emitted_at TIMESTAMPTZ NOT NULL
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "session_outcomes"
CONFLICT_KEY = "session_id"


class ModelSessionOutcomeEvent(BaseModel):
    """Inbound event from onex.evt.omniclaude.session-outcome.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str = Field(..., description="Unique session identifier.")
    outcome: str = Field(
        ..., description="Session outcome: success, failed, abandoned, unknown."
    )
    emitted_at: str | None = Field(default=None, description="ISO 8601 timestamp.")


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionSessionOutcome:
    """Project session-outcome events into session_outcomes table."""

    def project(
        self,
        event: ModelSessionOutcomeEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single session outcome event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "session_id": event.session_id,
            "outcome": event.outcome,
            "emitted_at": event.emitted_at or now,
            "ingested_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelSessionOutcomeEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of session outcome events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionSessionOutcome",
    "ModelProjectionResult",
    "ModelSessionOutcomeEvent",
]
