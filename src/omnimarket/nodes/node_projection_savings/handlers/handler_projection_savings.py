"""HandlerProjectionSavings — project savings-estimated events to DB.

Consumes onex.evt.omnibase-infra.savings-estimated.v1 and UPSERTs into
the savings_estimates table. SOW WARN blocker.

Target table schema (from omnidash migration, OMN-5552):
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  source_event_id TEXT UNIQUE NOT NULL
  session_id TEXT NOT NULL
  correlation_id TEXT
  schema_version TEXT DEFAULT '1.0'
  actual_total_tokens INT DEFAULT 0
  actual_cost_usd NUMERIC(12,10) DEFAULT 0
  actual_model_id TEXT
  counterfactual_model_id TEXT
  direct_savings_usd NUMERIC(12,10) DEFAULT 0
  direct_tokens_saved INT DEFAULT 0
  estimated_total_savings_usd NUMERIC(12,10) DEFAULT 0
  estimated_total_tokens_saved INT DEFAULT 0
  categories JSONB
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "savings_estimates"
CONFLICT_KEY = "source_event_id"


class ModelSavingsEstimatedEvent(BaseModel):
    """Inbound event from onex.evt.omnibase-infra.savings-estimated.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    source_event_id: str = Field(..., description="Unique event ID for dedup.")
    session_id: str = Field(..., description="Session ID.")
    correlation_id: str | None = Field(default=None)
    actual_total_tokens: int = Field(default=0, ge=0)
    actual_cost_usd: float = Field(default=0.0, ge=0.0)
    actual_model_id: str | None = Field(default=None)
    counterfactual_model_id: str | None = Field(default=None)
    direct_savings_usd: float = Field(default=0.0, ge=0.0)
    direct_tokens_saved: int = Field(default=0, ge=0)
    estimated_total_savings_usd: float = Field(default=0.0, ge=0.0)
    estimated_total_tokens_saved: int = Field(default=0, ge=0)
    categories: list[dict[str, object]] | None = Field(default=None)


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionSavings:
    """Project savings-estimated events into savings_estimates table."""

    def project(
        self,
        event: ModelSavingsEstimatedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single savings estimate event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "source_event_id": event.source_event_id,
            "session_id": event.session_id,
            "correlation_id": event.correlation_id,
            "schema_version": "1.0",
            "actual_total_tokens": event.actual_total_tokens,
            "actual_cost_usd": event.actual_cost_usd,
            "actual_model_id": event.actual_model_id,
            "counterfactual_model_id": event.counterfactual_model_id,
            "direct_savings_usd": event.direct_savings_usd,
            "direct_tokens_saved": event.direct_tokens_saved,
            "estimated_total_savings_usd": event.estimated_total_savings_usd,
            "estimated_total_tokens_saved": event.estimated_total_tokens_saved,
            "categories": event.categories,
            "ingested_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelSavingsEstimatedEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of savings events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionSavings",
    "ModelProjectionResult",
    "ModelSavingsEstimatedEvent",
]
