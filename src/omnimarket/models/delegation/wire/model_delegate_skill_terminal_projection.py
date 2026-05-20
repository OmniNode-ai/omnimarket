# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed projection models for delegate-skill terminal events."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

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
    prompt_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("prompt_text", "promptText", "prompt"),
    )
    model_cloud_baseline: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "model_cloud_baseline",
            "modelCloudBaseline",
            "baseline_model",
            "baselineModel",
        ),
    )

    @field_validator("repo_name", "prompt_text", "model_cloud_baseline")
    @classmethod
    def _blank_string_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

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
            event_timestamp=event.emitted_at,
            session_id=event.session_id or event.correlation_id,
            model_local=model_local,
            model_cloud_baseline=event.model_cloud_baseline or baseline_model,
            local_cost_usd=local_cost_usd,
            cloud_cost_usd=local_cost_usd + savings_usd,
            savings_usd=savings_usd,
            repo_name=event.repo_name,
            machine_id=event.machine_id,
        )


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
    "ModelDelegateSkillSavingsProjection",
    "ModelDelegateSkillTerminalProjection",
    "ModelDelegationEventProjectionRow",
    "ModelProjectionEnvelopeMetadata",
]
