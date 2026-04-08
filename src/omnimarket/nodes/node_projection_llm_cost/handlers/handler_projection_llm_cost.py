"""HandlerProjectionLlmCost — project LLM call events to cost aggregates.

Consumes onex.evt.omniintelligence.llm-call-completed.v1 and UPSERTs into
the llm_cost_aggregates table. SOW WARN blocker — cost data must flow.

Target table schema (from omnidash migration 0003):
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  bucket_time TIMESTAMPTZ NOT NULL
  granularity TEXT NOT NULL (hour | day)
  model_name TEXT NOT NULL
  session_id TEXT
  total_tokens INT DEFAULT 0
  prompt_tokens INT DEFAULT 0
  completion_tokens INT DEFAULT 0
  estimated_cost_usd NUMERIC(12,10) DEFAULT 0
  call_count INT DEFAULT 1
  usage_source TEXT DEFAULT 'API' (API | ESTIMATED | MISSING)
  ingested_at TIMESTAMPTZ DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "llm_cost_aggregates"
CONFLICT_KEY = "id"


class ModelLlmCallCompletedEvent(BaseModel):
    """Inbound event from onex.evt.omniintelligence.llm-call-completed.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    call_id: str = Field(..., description="Unique call identifier.")
    model_name: str = Field(default="unknown", description="LLM model name.")
    session_id: str | None = Field(default=None, description="Session ID.")
    total_tokens: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    usage_source: str = Field(default="API", description="API | ESTIMATED | MISSING.")
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp.")


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionLlmCost:
    """Project LLM call completed events into llm_cost_aggregates."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelLlmCallCompletedEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelLlmCallCompletedEvent(**input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelLlmCallCompletedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single LLM cost event as an hourly aggregate row."""
        now = datetime.now(tz=UTC)
        event_time = event.timestamp or now.isoformat()

        row: dict[str, object] = {
            "id": event.call_id,
            "bucket_time": event_time,
            "granularity": "hour",
            "model_name": event.model_name,
            "session_id": event.session_id,
            "total_tokens": event.total_tokens,
            "prompt_tokens": event.prompt_tokens,
            "completion_tokens": event.completion_tokens,
            "estimated_cost_usd": event.estimated_cost_usd,
            "call_count": 1,
            "usage_source": event.usage_source,
            "ingested_at": now.isoformat(),
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelLlmCallCompletedEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of LLM cost events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionLlmCost",
    "ModelLlmCallCompletedEvent",
    "ModelProjectionResult",
]
