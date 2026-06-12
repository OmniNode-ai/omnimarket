"""HandlerProjectionLlmCost — project LLM call events into llm_call_metrics.

Consumes onex.evt.omniintelligence.llm-call-completed.v1 and writes a per-call
row into the ``llm_call_metrics`` table (the dashboard read model). SOW WARN
blocker — cost data must flow.

OMN-13001: this handler previously wrote ``llm_cost_aggregates`` with a drifted
column set that no longer matched the deployed table, so it landed nothing. It
now writes ``llm_call_metrics`` through the shared per-call row builder
(row_llm_call_metrics.build_llm_call_metrics_row) — the same single write
authority the deployed runtime writer (handler_llm_cost.LlmCostProjectionRunner)
and the backfill entrypoint use. The aggregate read model is owned by
node_projection_cost_summary, not this node.

This sync ``project()`` path is the in-process/RuntimeLocal shim (used by the
golden-chain test and cli_delegation_cost_demo). The deployed Kafka writer is
handler_llm_cost.LlmCostProjectionRunner. Both write the same column set
(asserted by the schema-parity ratchet).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_projection_llm_cost.handlers.row_llm_call_metrics import (
    build_llm_call_metrics_row,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "llm_call_metrics"
CONFLICT_KEY = "input_hash"

TOPIC_LLM_CALL_COMPLETED: str = "onex.evt.omniintelligence.llm-call-completed.v1"  # onex-topic-allow: pending contract auto-wiring


class ModelLlmCallCompletedEvent(BaseModel):
    """Inbound event from onex.evt.omniintelligence.llm-call-completed.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    call_id: str = Field(
        default="",
        validation_alias=AliasChoices("call_id", "correlation_id", "input_hash"),
        description="Unique call identifier.",
    )
    model_name: str = Field(
        default="unknown",
        validation_alias=AliasChoices("model_name", "model_id"),
        description="LLM model name.",
    )
    session_id: str | None = Field(default=None, description="Session ID.")
    total_tokens: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("prompt_tokens", "input_tokens"),
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("completion_tokens", "output_tokens"),
    )
    estimated_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("estimated_cost_usd", "cost_usd"),
    )
    usage_source: EnumUsageSource = Field(default=EnumUsageSource.MEASURED)
    gpu_seconds: float | None = Field(default=None, ge=0.0)
    gpu_type: str | None = Field(default=None, max_length=64)
    gpu_count: int | None = Field(default=None, ge=0)
    compute_usage_source: EnumUsageSource | None = Field(default=None)
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp.")


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionLlmCost:
    """Project LLM call completed events into llm_call_metrics (per-call read model)."""

    def __init__(self, pricing_manifest_path: str | Path | None = None) -> None:
        self._pricing_manifest_path = (
            Path(pricing_manifest_path) if pricing_manifest_path is not None else None
        )

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
        """Write a single LLM call event as a per-call llm_call_metrics row.

        Cost folds compute cost (GPU electricity + amortization, from the pricing
        manifest) into the persisted ``estimated_cost_usd`` so locally-served
        model calls carry an honest dollar figure rather than 0.
        """
        compute_cost_usd = self._compute_cost_usd(event)
        event_payload: dict[str, Any] = event.model_dump(mode="json")
        event_payload["estimated_cost_usd"] = round(
            event.estimated_cost_usd + compute_cost_usd, 10
        )
        row = build_llm_call_metrics_row(event_payload)
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelLlmCallCompletedEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Write a batch of LLM cost events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)

    def _compute_cost_usd(self, event: ModelLlmCallCompletedEvent) -> float:
        if (
            event.gpu_seconds is None
            or event.gpu_type is None
            or event.gpu_count is None
            or event.gpu_count == 0
        ):
            return 0.0

        rates = self._load_compute_cost_rates()
        rate = rates.get(event.gpu_type)
        if rate is None:
            return 0.0

        hourly_rate = rate["electricity_per_hour"] + rate["amortization_per_hour"]
        return round((event.gpu_seconds / 3600.0) * hourly_rate * event.gpu_count, 10)

    def _load_compute_cost_rates(self) -> dict[str, dict[str, float]]:
        manifest_path = self._pricing_manifest_path or _default_pricing_manifest_path()
        if manifest_path is None or not manifest_path.exists():
            return {}

        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        raw_compute_cost = raw.get("compute_cost", {})
        if not isinstance(raw_compute_cost, dict):
            return {}

        rates: dict[str, dict[str, float]] = {}
        for gpu_type, entry in raw_compute_cost.items():
            if not isinstance(gpu_type, str) or not isinstance(entry, dict):
                continue
            electricity = _float_or_none(entry.get("electricity_per_hour"))
            amortization = _float_or_none(entry.get("amortization_per_hour"))
            if electricity is None or amortization is None:
                continue
            rates[gpu_type] = {
                "electricity_per_hour": electricity,
                "amortization_per_hour": amortization,
            }
        return rates


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_pricing_manifest_path() -> Path | None:
    import os

    configured = os.environ.get("OMNI_PRICING_MANIFEST_PATH")  # contract-config-ok: config  # fmt: skip
    if configured:
        return Path(configured)

    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        return (
            Path(omni_home)
            / "omnibase_infra"
            / "src"
            / "omnibase_infra"
            / "configs"
            / "pricing_manifest.yaml"
        )
    return None


__all__: list[str] = [
    "TOPIC_LLM_CALL_COMPLETED",
    "HandlerProjectionLlmCost",
    "ModelLlmCallCompletedEvent",
    "ModelProjectionResult",
]
