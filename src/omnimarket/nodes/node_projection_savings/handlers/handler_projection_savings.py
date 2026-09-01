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
    DELEGATION_COMPLETED_TOPIC_V1,
    DELEGATION_FAILED_TOPIC_V1,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
    ModelTaskDelegatedSavingsSource,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SAVINGS_METHOD_VALUES,
    USAGE_SOURCE_VALUES,
    _normalize_savings_estimate_payload,
    provenance_or_none,
)
from omnimarket.pricing import DEFAULT_BASELINE_MODEL, build_premium_counterfactual
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "savings_estimates"
CONFLICT_KEY = "session_id,event_timestamp,model_local,model_cloud_baseline"

# Fields ModelSavingsEstimatedEvent (extra="forbid") declares — used to strip
# _normalize_savings_estimate_payload's additive output (old + new keys) back
# down to a constructible shape (OMN-14533).
_CANONICAL_SAVINGS_FIELDS: frozenset[str] = frozenset(
    {
        "event_timestamp",
        "session_id",
        "model_local",
        "model_cloud_baseline",
        "local_cost_usd",
        "cloud_cost_usd",
        "savings_usd",
        "repo_name",
        "machine_id",
        # OMN-15533: pass through the task class and served token counts when the
        # producer supplies them, instead of dropping them at this filter.
        "task_type",
        "prompt_tokens",
        "completion_tokens",
        # OMN-15533 (AC3, second pass): the producer's OWN provenance. Dropping
        # these here is what forced the read view to invent a replacement, and the
        # replacement it settled on (tokens > 0 -> 'measured') relabels every
        # estimate that carries token counts as a measurement.
        "savings_method",
        "usage_source",
        "pricing_manifest_version",
    }
)


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
    # OMN-15533: optional because the real producer (ModelSavingsEstimate) carries
    # neither. None persists as NULL — "not recorded" — which the read view renders
    # as an absent task class with estimated/unknown provenance, never as a model
    # name and never as a measured 0.
    task_type: str | None = Field(
        default=None,
        description="Task class from the source terminal. Never a model identifier.",
    )
    prompt_tokens: int | None = Field(
        default=None, ge=0, description="Served input tokens; None if not recorded."
    )
    completion_tokens: int | None = Field(
        default=None, ge=0, description="Served output tokens; None if not recorded."
    )
    # OMN-15533 (AC3, second pass): the provenance the SOURCE stated. The real
    # producer ships all three on every event (ModelSavingsEstimate.is_measured /
    # .usage_source / .pricing_manifest_version) and they were being dropped at
    # this seam, leaving the read view to infer a provenance from token counts —
    # which labelled estimate-derived rows 'measured' the moment they carried any
    # tokens. None persists as NULL and is read back as a refusal, never as a
    # measurement claim.
    savings_method: str | None = Field(
        default=None,
        description=(
            "How the saving was obtained, as stated by the source event: "
            "'measured' or 'estimated'. None if the source did not state it."
        ),
    )
    usage_source: str | None = Field(
        default=None,
        description=(
            "Cost provenance as stated by the source event: 'measured', "
            "'estimated' or 'unknown'. None if the source did not state it."
        ),
    )
    pricing_manifest_version: str | None = Field(
        default=None,
        description=("Pricing manifest the source priced with. None if not recorded."),
    )

    @field_validator("savings_method")
    @classmethod
    def validate_savings_method(cls, value: str | None) -> str | None:
        return provenance_or_none(value, SAVINGS_METHOD_VALUES)

    @field_validator("usage_source")
    @classmethod
    def validate_usage_source(cls, value: str | None) -> str | None:
        return provenance_or_none(value, USAGE_SOURCE_VALUES)

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
                "delegate_skill_baseline_model", DEFAULT_BASELINE_MODEL
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

        # OMN-13629 (WS-F Phase 1): canonical delegation terminal SOURCE event
        # (delegation-{completed,failed}.v1) -> savings_estimates. Repointed off
        # the legacy compat task-delegated.v1 (OMN-12494 / OMN-13598 stopgap). The
        # canonical ModelDelegationResult carries the measured cumulative cost +
        # served tokens; the cloud-baseline counterfactual is re-derived from
        # those served tokens so the saving stays a measurement, not an estimate.
        if (
            event_type in {DELEGATION_COMPLETED_TOPIC_V1, DELEGATION_FAILED_TOPIC_V1}
            or "delegation-completed" in event_type
            or "delegation-failed" in event_type
            or _is_canonical_delegation_source_payload(payload)
        ):
            source = ModelTaskDelegatedSavingsSource.from_canonical_payload(
                payload,
                counterfactual_builder=build_premium_counterfactual,
            )
            projection = ModelDelegateSkillSavingsProjection.from_task_delegated_event(
                source,
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
        # OMN-14533: the real onex.evt.omnibase-infra.savings-estimated.v1
        # producer (ModelSavingsEstimate) never matches this model's field
        # names (actual_model_id/counterfactual_model_id/actual_cost_usd/
        # estimated_total_savings_usd/timestamp_iso vs this model's
        # model_local/model_cloud_baseline/local_cost_usd/savings_usd/
        # event_timestamp) — every real event hit extra="forbid" plus
        # multiple missing-required-field errors simultaneously. Normalize
        # onto the canonical shape, then keep only the fields
        # ModelSavingsEstimatedEvent (extra="forbid") actually declares —
        # normalization is additive (old + new keys), so the raw
        # ModelSavingsEstimate keys must be dropped before construction.
        event_data = _normalize_savings_estimate_payload(event_data)
        event_data = {
            key: event_data[key]
            for key in _CANONICAL_SAVINGS_FIELDS
            if key in event_data
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
        # OMN-15533: only stamp the task class / token counts the producer actually
        # sent. Omitting the key leaves the savings_estimates column NULL on INSERT
        # and leaves an already-known value untouched on UPDATE — the same
        # convention project_delegate_skill_savings uses for tenant_id, and the
        # reason a savings-estimated event cannot blank out counts a richer
        # terminal already established for the same row.
        if event.task_type:
            row["task_type"] = event.task_type
        if event.prompt_tokens is not None:
            row["prompt_tokens"] = event.prompt_tokens
        if event.completion_tokens is not None:
            row["completion_tokens"] = event.completion_tokens
        # OMN-15533: same omit-when-absent convention. A source that stated no
        # provenance leaves the column NULL, which the read view renders as
        # estimated/unknown; it must never overwrite a provenance a richer
        # terminal already established for the same row.
        if event.savings_method is not None:
            row["savings_method"] = event.savings_method
        if event.usage_source is not None:
            row["usage_source"] = event.usage_source
        if event.pricing_manifest_version:
            row["pricing_manifest_version"] = event.pricing_manifest_version
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
            # OMN-15533: a delegation terminal always knows its task class and the
            # tokens it served — that is precisely the data the view was
            # substituting model_local and a hardcoded 0 for.
            "task_type": projection.task_type,
            "prompt_tokens": projection.prompt_tokens,
            "completion_tokens": projection.completion_tokens,
            # OMN-15533: the writer states the provenance, because the writer is
            # the only party that knows how the number was produced. A terminal
            # whose token basis is recorded yields OMN-13629's measurement; one
            # without refuses rather than claiming it. The read view no longer
            # guesses from token presence.
            "savings_method": projection.savings_method,
            "usage_source": projection.usage_source,
        }
        # OMN-14058 (OPERATOR-ACCEPTED INTERIM): only stamp tenant_id when the
        # source projection carried one — omitting the key lets the
        # savings_estimates column DEFAULT 'omninode' apply on INSERT and
        # leaves an already-known tenant untouched on UPDATE.
        if projection.tenant_id:
            row["tenant_id"] = projection.tenant_id
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


def _is_canonical_delegation_source_payload(payload: dict[str, object]) -> bool:
    """Discriminate a canonical delegation terminal SOURCE payload (OMN-13629).

    The canonical ``ModelDelegationResult`` terminal carries ``correlation_id`` +
    ``task_type`` + ``model_used`` but neither the delegate-skill terminal
    ``status``/``metrics`` shape nor the savings-estimated
    ``local_cost_usd``/``cloud_cost_usd`` shape. Unlike the legacy compat
    task-delegated event, it does NOT carry a pinned ``premium_counterfactual``
    — that baseline is re-derived from the served tokens downstream. The positive
    signal is ``model_used`` (the canonical terminal's model field) together with
    the absence of the other two shapes.
    """
    return (
        payload.get("correlation_id") is not None
        and payload.get("task_type") is not None
        and payload.get("model_used") is not None
        and payload.get("status") is None
        and "metrics" not in payload
        and "local_cost_usd" not in payload
    )
