"""HandlerProjectionSavings — project savings-estimated events to DB."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.events.topics import (
    DELEGATE_SKILL_COMPLETED_TOPIC_V1,
    DELEGATE_SKILL_FAILED_TOPIC_V1,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "savings_estimates"
CONFLICT_KEY = "session_id,event_timestamp,model_local,model_cloud_baseline"


class ModelSavingsEstimatedEvent(BaseModel):
    """Inbound event from onex.evt.omnibase-infra.savings-estimated.v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_timestamp: datetime = Field(description="UTC event time for the estimate.")
    session_id: str = Field(min_length=1, description="Session ID.")
    model_local: str = Field(min_length=1, description="Observed local model.")
    model_cloud_baseline: str = Field(
        min_length=1, description="Cloud model baseline for the counterfactual."
    )
    local_cost_usd: Decimal = Field(ge=Decimal("0"))
    cloud_cost_usd: Decimal = Field(ge=Decimal("0"))
    savings_usd: Decimal
    repo_name: str | None = Field(default=None)
    machine_id: str | None = Field(default=None)

    @field_validator("event_timestamp")
    @classmethod
    def validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_savings_consistency(self) -> ModelSavingsEstimatedEvent:
        if self.savings_usd != self.cloud_cost_usd - self.local_cost_usd:
            raise ValueError("savings_usd must equal cloud_cost_usd - local_cost_usd")
        return self


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionSavings:
    """Project savings-estimated events into savings_estimates table."""

    _delegate_skill_terminal_events = frozenset(
        {
            "delegate-skill-completed",
            "delegate-skill-failed",
            DELEGATE_SKILL_COMPLETED_TOPIC_V1,
            DELEGATE_SKILL_FAILED_TOPIC_V1,
        }
    )

    def __init__(self, contract_path: Path | None = None) -> None:
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            contract: dict[str, Any] = yaml.safe_load(f)
        self._delegate_skill_baseline_model = str(
            contract.get("metadata", {}).get(
                "delegate_skill_baseline_model", "claude-sonnet-4-6"
            )
        )

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelSavingsEstimatedEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event_type = str(payload.pop("_event_type", ""))
        if (
            event_type in self._delegate_skill_terminal_events
            or _is_delegate_skill_terminal_payload(payload)
        ):
            terminal = ModelDelegateSkillTerminalProjection.from_payload(payload)
            projection = ModelDelegateSkillSavingsProjection.from_terminal_event(
                terminal,
                baseline_model=self._delegate_skill_baseline_model,
            )
            if projection is None:
                return ModelProjectionResult(rows_upserted=0).model_dump(mode="json")
            result = self.project_delegate_skill_savings(projection, db_raw)
            return result.model_dump(mode="json")

        event_data = {
            key: value
            for key, value in payload.items()
            if not key.startswith("_")
            and key not in {"rows", "event_landed", "latency_ms"}
        }
        event = ModelSavingsEstimatedEvent(**event_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelSavingsEstimatedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single savings estimate event."""
        now = datetime.now(tz=UTC).isoformat()
        event_timestamp = event.event_timestamp.astimezone(UTC).isoformat()
        row: dict[str, object] = {
            "event_timestamp": event_timestamp,
            "session_id": event.session_id,
            "model_local": event.model_local,
            "model_cloud_baseline": event.model_cloud_baseline,
            "local_cost_usd": event.local_cost_usd,
            "cloud_cost_usd": event.cloud_cost_usd,
            "savings_usd": event.savings_usd,
            "repo_name": event.repo_name,
            "machine_id": event.machine_id,
            "created_at": now,
            "updated_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_delegate_skill_savings(
        self,
        projection: ModelDelegateSkillSavingsProjection,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT savings_estimates from a typed delegate-skill terminal event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "event_timestamp": projection.event_timestamp.astimezone(UTC).isoformat(),
            "session_id": str(projection.session_id),
            "model_local": projection.model_local,
            "model_cloud_baseline": projection.model_cloud_baseline,
            "local_cost_usd": projection.local_cost_usd,
            "cloud_cost_usd": projection.cloud_cost_usd,
            "savings_usd": projection.savings_usd,
            "repo_name": projection.repo_name,
            "machine_id": (
                str(projection.machine_id)
                if projection.machine_id is not None
                else None
            ),
            "created_at": now,
            "updated_at": now,
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


def _is_delegate_skill_terminal_payload(payload: dict[str, object]) -> bool:
    return (
        payload.get("correlation_id") is not None
        and payload.get("status") is not None
        and isinstance(payload.get("metrics"), dict)
    )
