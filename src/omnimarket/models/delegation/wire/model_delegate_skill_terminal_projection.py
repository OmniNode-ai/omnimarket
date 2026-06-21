# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed projection models for delegate-skill terminal events."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual
from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)

# Materializer provenance versions (OMN-12606 / OMN-12488 acceptance-extension).
# PROJECTION_VERSION identifies the projection/schema contract of the
# delegation_events row; REDUCER_VERSION identifies the reducer logic that
# materialized it. Both are stamped on every reducer-materialized row so the
# proof packet can attribute a fresh terminal delegation event to a known
# materializer without an operator backfill.
PROJECTION_VERSION = "1.0.0"
REDUCER_VERSION = "1.0.0"


class ModelProjectionEnvelopeMetadata(BaseModel):
    """Subset of ONEX envelope metadata used by projection materializers."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    envelope_timestamp: AwareDatetime | None = Field(default=None)


class ModelDelegateSkillTerminalProjection(ModelDelegateSkillResponse):
    """Delegate-skill terminal event plus projection-owned metadata."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    emitted_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        validation_alias=AliasChoices("emitted_at", "emittedAt", "timestamp"),
    )
    session_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("session_id", "sessionId"),
    )
    machine_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("machine_id", "machineId"),
    )
    repo_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("repo_name", "repoName", "repo"),
    )
    prompt_text: str = Field(
        default="",
        validation_alias=AliasChoices("prompt_text", "promptText", "prompt"),
    )
    model_cloud_baseline: str = Field(
        default="",
        validation_alias=AliasChoices(
            "model_cloud_baseline",
            "modelCloudBaseline",
            "baseline_model",
            "baselineModel",
        ),
    )

    @field_validator("repo_name")
    @classmethod
    def _blank_string_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("prompt_text")
    @classmethod
    def _blank_prompt_to_empty(cls, value: str) -> str:
        return value.strip()

    @field_validator("model_cloud_baseline")
    @classmethod
    def _blank_baseline_to_empty(cls, value: str) -> str:
        return value.strip()

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> ModelDelegateSkillTerminalProjection:
        """Validate broker payload using the declared terminal response model."""
        return cls.model_validate(_payload_with_envelope_timestamp(payload))


class ModelDelegationEventProjectionRow(BaseModel):
    """Typed row for delegation_events upserts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    session_id: UUID | None = None
    timestamp: AwareDatetime
    task_type: str
    delegated_to: str
    model_name: str
    delegated_by: str
    quality_gate_passed: bool
    quality_gates_checked: tuple[str, ...]
    quality_gates_failed: tuple[str, ...]
    quality_gate_detail: str
    cost_usd: Decimal
    cost_savings_usd: Decimal
    latency_ms: int
    repo_name: str | None = None
    is_shadow: bool = False
    prompt_text: str | None = None
    response_text: str | None = None
    tokens_input: int
    tokens_output: int
    tokens_to_compliance: int
    compliance_attempts: int
    pricing_manifest_version: int = 0
    premium_counterfactual: ModelPremiumCounterfactual | None = None
    projection_version: str = PROJECTION_VERSION
    reducer_version: str = REDUCER_VERSION

    @classmethod
    def from_terminal_event(
        cls,
        event: ModelDelegateSkillTerminalProjection,
    ) -> ModelDelegationEventProjectionRow:
        metrics = event.metrics
        delegated_to = event.model_name or event.provider or "delegate-skill"
        tokens_to_compliance = metrics.tokens_to_compliance or metrics.total_tokens
        quality_detail = event.error_message or event.status
        return cls(
            correlation_id=event.correlation_id,
            session_id=event.session_id,
            timestamp=event.emitted_at,
            task_type=event.task_type,
            delegated_to=delegated_to,
            model_name=event.model_name,
            delegated_by="delegate-skill-orchestrator",
            quality_gate_passed=event.quality_gate_passed,
            quality_gates_checked=("delegate-skill-terminal",),
            quality_gates_failed=tuple(event.quality_gates_failed),
            quality_gate_detail=quality_detail,
            cost_usd=Decimal(str(metrics.cost_usd)),
            cost_savings_usd=Decimal(str(metrics.cost_savings_usd)),
            latency_ms=metrics.latency_ms,
            repo_name=event.repo_name,
            prompt_text=event.prompt_text,
            response_text=event.response or event.error_message or None,
            tokens_input=metrics.input_tokens,
            tokens_output=metrics.output_tokens,
            tokens_to_compliance=tokens_to_compliance,
            compliance_attempts=metrics.compliance_attempts,
            pricing_manifest_version=event.pricing_manifest_version,
            # OMN-13355: carry the pinned premium counterfactual onto the row.
            premium_counterfactual=metrics.premium_counterfactual,
            projection_version=PROJECTION_VERSION,
            reducer_version=REDUCER_VERSION,
        )


class ModelDelegateSkillSavingsProjection(BaseModel):
    """Typed row for savings_estimates derived from a terminal delegation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_timestamp: AwareDatetime
    session_id: UUID
    model_local: str
    model_cloud_baseline: str
    local_cost_usd: Decimal
    cloud_cost_usd: Decimal
    savings_usd: Decimal
    repo_name: str | None = None
    machine_id: UUID | None = None

    @model_validator(mode="after")
    def _amounts_match(self) -> Self:
        if self.savings_usd != self.cloud_cost_usd - self.local_cost_usd:
            raise ValueError("savings_usd must equal cloud_cost_usd - local_cost_usd")
        return self

    @classmethod
    def from_terminal_event(
        cls,
        event: ModelDelegateSkillTerminalProjection,
        *,
        baseline_model: str,
    ) -> ModelDelegateSkillSavingsProjection | None:
        savings_usd = Decimal(str(event.metrics.cost_savings_usd))
        if savings_usd <= 0:
            return None
        local_cost_usd = Decimal(str(event.metrics.cost_usd))
        model_local = event.model_name or event.provider
        if not model_local:
            return None
        return cls(
            event_timestamp=event.emitted_at.replace(microsecond=0),
            session_id=event.correlation_id,
            model_local=model_local,
            model_cloud_baseline=event.model_cloud_baseline or baseline_model,
            local_cost_usd=local_cost_usd,
            cloud_cost_usd=local_cost_usd + savings_usd,
            savings_usd=savings_usd,
            repo_name=event.repo_name,
            machine_id=event.machine_id,
        )

    @classmethod
    def from_task_delegated_event(
        cls,
        event: ModelTaskDelegatedSavingsSource,
        *,
        baseline_model: str,
    ) -> ModelDelegateSkillSavingsProjection | None:
        """Materialize a savings_estimates row from a canonical task-delegated
        SOURCE event (OMN-12494).

        This closes the projection gap: previously savings_estimates was only
        materialized from the derived delegate-skill terminal event, so the
        Delegation Savings widget stayed truthfully-empty for the canonical
        ``onex.evt.omniclaude.task-delegated.v1`` stream that drives
        delegation_events. The figures are MEASUREMENTS, not estimates — the
        local cost is the serving tier's measured actual cost (OMN-13355) and the
        cloud baseline is the pinned premium counterfactual:

            local_cost_usd  = cost_usd                         (measured actual)
            cloud_cost_usd  = counterfactual_cost_usd          (pinned baseline)
            savings_usd     = cloud_cost_usd - local_cost_usd

        Returns ``None`` (truthful no-row) when there is no pinned premium
        counterfactual to defend the saving, or the resulting saving is <= 0 —
        the dashboard's truthful-empty state is preserved, never fixture data.
        """
        counterfactual = event.premium_counterfactual
        if counterfactual is None:
            return None
        local_cost_usd = Decimal(str(event.cost_usd))
        cloud_cost_usd = counterfactual.counterfactual_cost_usd
        savings_usd = cloud_cost_usd - local_cost_usd
        if savings_usd <= 0:
            return None
        model_local = event.model_name or event.delegated_to
        if not model_local:
            return None
        return cls(
            event_timestamp=event.resolved_timestamp().replace(microsecond=0),
            session_id=event.correlation_id,
            model_local=model_local,
            model_cloud_baseline=counterfactual.model or baseline_model,
            local_cost_usd=local_cost_usd,
            cloud_cost_usd=cloud_cost_usd,
            savings_usd=savings_usd,
            repo_name=event.repo,
            machine_id=None,
        )


class ModelTaskDelegatedSavingsSource(BaseModel):
    """Canonical task-delegated SOURCE event fields needed to materialize a
    savings_estimates row (OMN-12494).

    Parsed from ``onex.evt.omniclaude.task-delegated.v1`` — the same stream that
    drives the delegation_events projection. Carries the measured actual cost
    (OMN-13355) and the pinned premium counterfactual (OMN-13355) so the savings
    materialization is a measurement, not an estimate. ``extra="ignore"`` so the
    rich task-delegated payload (quality gates, tokens, scores) parses cleanly.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: UUID = Field(
        ..., description="Unique correlation ID; the savings row identity key."
    )
    task_type: str = Field(..., min_length=1)
    delegated_to: str = Field(default="")
    model_name: str = Field(default="")
    repo: str | None = Field(default=None)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    premium_counterfactual: ModelPremiumCounterfactual | None = Field(default=None)
    timestamp: AwareDatetime | None = Field(default=None)

    @field_validator("repo", mode="before")
    @classmethod
    def _blank_repo_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def resolved_timestamp(self) -> datetime:
        """Event timestamp, defaulting to now() only when the source omits it."""
        return self.timestamp or datetime.now(tz=UTC)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> ModelTaskDelegatedSavingsSource:
        return cls.model_validate(_payload_with_envelope_timestamp(payload))


def _payload_with_envelope_timestamp(
    payload: Mapping[str, object],
) -> dict[str, object]:
    normalized = {key: value for key, value in payload.items() if key != "_envelope"}
    if not _has_timestamp(normalized):
        envelope = payload.get("_envelope")
        if isinstance(envelope, Mapping):
            metadata = ModelProjectionEnvelopeMetadata.model_validate(envelope)
            if metadata.envelope_timestamp is not None:
                normalized["emitted_at"] = metadata.envelope_timestamp
    return normalized


def _has_timestamp(payload: Mapping[str, object]) -> bool:
    return any(
        payload.get(key) is not None for key in ("emitted_at", "emittedAt", "timestamp")
    )


__all__ = [
    "PROJECTION_VERSION",
    "REDUCER_VERSION",
    "ModelDelegateSkillSavingsProjection",
    "ModelDelegateSkillTerminalProjection",
    "ModelDelegationEventProjectionRow",
    "ModelProjectionEnvelopeMetadata",
    "ModelTaskDelegatedSavingsSource",
]
