# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionLlmRouting — project contract-routed LLM decisions to DB.

Consumes contract-declared routing decision events and UPSERTs into the
llm_routing_decisions table. Dedup by correlation_id.

Target table schema (from omnibase_infra migration 065_create_llm_routing_decisions.sql):
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  correlation_id UUID UNIQUE NOT NULL
  session_id TEXT
  llm_agent TEXT NOT NULL
  fuzzy_agent TEXT
  agreement BOOLEAN NOT NULL DEFAULT FALSE
  llm_confidence NUMERIC(5,4)
  fuzzy_confidence NUMERIC(5,4)
  llm_latency_ms INTEGER NOT NULL DEFAULT 0
  fuzzy_latency_ms INTEGER NOT NULL DEFAULT 0
  used_fallback BOOLEAN NOT NULL DEFAULT FALSE
  routing_prompt_version TEXT NOT NULL DEFAULT 'unknown'
  intent TEXT
  model TEXT
  cost_usd NUMERIC(12,8)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "llm_routing_decisions"
CONFLICT_KEY = "correlation_id"


class ModelLlmRoutingDecisionEvent(BaseModel):
    """Inbound event payload for the contract-declared routing decision topic."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    correlation_id: str = Field(
        ..., description="Distributed tracing UUID. UPSERT conflict target."
    )
    session_id: str | None = Field(default=None)
    llm_agent: str = Field(
        ...,
        validation_alias=AliasChoices("llm_agent", "selected_agent"),
        description="Agent name selected by LLM routing.",
    )
    fuzzy_agent: str | None = Field(
        default=None, description="Agent selected by fuzzy matching."
    )
    agreement: bool = Field(
        default=False, description="True when LLM and fuzzy routing agreed."
    )
    llm_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    fuzzy_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    llm_latency_ms: int = Field(default=0, ge=0)
    fuzzy_latency_ms: int = Field(default=0, ge=0)
    used_fallback: bool = Field(default=False)
    routing_prompt_version: str = Field(
        default="unknown",
        validation_alias=AliasChoices("routing_prompt_version", "prompt_version"),
    )
    intent: str | None = Field(default=None)
    model: str | None = Field(default=None)
    cost_usd: float | None = Field(default=None, ge=0.0)
    timestamp: str | None = Field(default=None, description="ISO 8601 event timestamp.")


class ModelProjectionResult(BaseModel):
    """Result of a projection operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionLlmRouting:
    """Project llm-routing-decision events into llm_routing_decisions."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim."""
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelLlmRoutingDecisionEvent(**input_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelLlmRoutingDecisionEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single LLM routing decision event row."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            "llm_agent": event.llm_agent,
            "fuzzy_agent": event.fuzzy_agent,
            "agreement": event.agreement,
            "llm_confidence": event.llm_confidence,
            "fuzzy_confidence": event.fuzzy_confidence,
            "llm_latency_ms": event.llm_latency_ms,
            "fuzzy_latency_ms": event.fuzzy_latency_ms,
            "used_fallback": event.used_fallback,
            "routing_prompt_version": event.routing_prompt_version,
            "intent": event.intent,
            "model": event.model,
            "cost_usd": event.cost_usd,
            "projected_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelLlmRoutingDecisionEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of routing decision events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "HandlerProjectionLlmRouting",
    "ModelLlmRoutingDecisionEvent",
    "ModelProjectionResult",
]
