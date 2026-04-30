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
  usage_source TEXT DEFAULT 'measured'
  ingested_at TIMESTAMPTZ DEFAULT NOW()
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "llm_cost_aggregates"
CONFLICT_KEY = "id"


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
    """Project LLM call completed events into llm_cost_aggregates."""

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
        """UPSERT a single LLM cost event as an hourly aggregate row."""
        now = datetime.now(tz=UTC)
        event_time = event.timestamp or now.isoformat()
        call_id = event.call_id or f"{event.model_name}:{event.session_id}:{event_time}"
        compute_cost_usd = self._compute_cost_usd(event)

        row: dict[str, object] = {
            "id": call_id,
            "bucket_time": event_time,
            "granularity": "hour",
            "model_name": event.model_name,
            "session_id": event.session_id,
            "total_tokens": event.total_tokens,
            "prompt_tokens": event.prompt_tokens,
            "completion_tokens": event.completion_tokens,
            "estimated_cost_usd": event.estimated_cost_usd,
            "compute_cost_usd": compute_cost_usd,
            "total_cost_usd": round(event.estimated_cost_usd + compute_cost_usd, 10),
            "call_count": 1,
            "usage_source": event.usage_source.value,
            "compute_usage_source": (
                event.compute_usage_source.value
                if event.compute_usage_source is not None
                else None
            ),
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

    configured = os.environ.get("OMNI_PRICING_MANIFEST_PATH")
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
    "HandlerProjectionLlmCost",
    "ModelLlmCallCompletedEvent",
    "ModelProjectionResult",
]
