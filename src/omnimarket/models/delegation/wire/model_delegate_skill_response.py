# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-facing delegate-skill response model."""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumDelegationTerminalFailureCause,
    EnumQualityScoreComparison,
    ModelPremiumCounterfactual,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDelegateSkillAttemptRecord(BaseModel):
    """One tier/backend attempt in a delegation's escalation ladder (OMN-14063).

    Populated for the bus-less local dispatch path from the per-attempt list
    ``LocalDelegationDispatchPort.dispatch`` already builds internally; prior to
    OMN-14063 that list was computed but never threaded onto the typed response,
    so a local->cloud escalation (e.g. triggered by a flaky health probe) was
    invisible to the caller — visible only by grepping the capture-file log.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: str = Field(...)
    backend_id: str = Field(...)
    model_id: str = Field(...)
    quality_gate_passed: bool = Field(...)
    quality_score: float | None = Field(default=None)
    cost_usd: float = Field(default=0.0, ge=0.0)
    failure_class: str | None = Field(
        default=None,
        description="Transport failure_class (e.g. 'model_unavailable') when this "
        "attempt was skipped/failed before inference ran; None for a quality-gate "
        "verdict or a successful attempt.",
    )
    error_message: str = Field(
        default="",
        description="Why this tier was skipped/failed, e.g. 'endpoint <url> failed "
        "health probe' — the same reason previously visible only in the capture log.",
    )


class ModelDelegateSkillResponseMetrics(BaseModel):
    """Cost and latency metrics for a delegation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    tokens_to_compliance: int = Field(default=0, ge=0)
    compliance_attempts: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    cost_savings_usd: float = Field(default=0.0, ge=0.0)
    frontier_costs_usd: dict[str, float] = Field(default_factory=dict)
    premium_counterfactual: ModelPremiumCounterfactual | None = Field(
        default=None,
        description=(
            "Pinned premium counterfactual {model, price, as_of, tokens, cost} "
            "(OMN-13355). cost_savings_usd = counterfactual_cost_usd - cost_usd."
        ),
    )
    latency_ms: int = Field(default=0, ge=0)


class ModelDelegateSkillResponse(BaseModel):
    """Typed delegation result returned to requesting adapters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["completed", "failed", "timeout"] = Field(...)
    correlation_id: UUID = Field(...)
    task_type: str = Field(...)
    # string-id-ok: tenant_id is a named tenant identifier (slug), not a UUID.
    # OMN-14485: the terminal event `delegate-skill-completed.v1` is auto-published
    # from this response, and node_projection_delegation reads the row's tenant
    # from that terminal. Before this field the response could not carry the
    # request-resolved tenant, so the terminal was tenant-less and every row fell
    # back to the 'omninode' column default -- a LIVE NO-OP for tenant-carry on the
    # merged multitenant write-path (OMN-14208 epic). None means no tenant was
    # resolved (request tenant_id absent AND ONEX_TENANT_ID unset); the projection
    # then applies the column default. The verified value is resolved upstream and
    # via the ONEX_TENANT_ID interim (OMN-14058), never self-reported here.
    tenant_id: str | None = Field(
        default=None,
        description=(
            "Multi-tenant isolation identifier carried onto the terminal event so "
            "the delegation_events projection row stamps a real tenant. None means "
            "the 'omninode' column default applies."
        ),
    )
    provider: str = Field(default="")
    model_name: str = Field(default="")
    model_cloud_baseline: str = Field(default="")
    pricing_manifest_version: int = Field(default=0, ge=0)
    prompt_text: str = Field(default="")
    response: str = Field(default="")
    quality_gate_passed: bool = Field(default=False)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    required_quality_bar: float | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        ge=0.0,
        le=1.0,
        description="Authoritative minimum quality score applied to this result.",
    )
    score_vs_required_bar: EnumQualityScoreComparison | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Typed comparison between quality_score and its required bar.",
    )
    failed_acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        description="Authoritative quality-gate criteria that rejected this result.",
    )
    terminal_failure_cause: EnumDelegationTerminalFailureCause | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Stable machine-readable terminal failure cause, when known.",
    )
    quality_gates_failed: list[str] = Field(default_factory=list)
    metrics: ModelDelegateSkillResponseMetrics = Field(
        default_factory=ModelDelegateSkillResponseMetrics,
    )
    error_message: str = Field(default="")
    escalation_count: int = Field(
        default=0,
        ge=0,
        description="Number of up-tier escalations before the terminal attempt "
        "(OMN-14063). 0 means the first-resolved tier answered directly.",
    )
    attempts_count: int = Field(
        default=1,
        ge=1,
        description="Authoritative total inference calls including compliance "
        "repairs, same-tier retries, and the terminal attempt.",
    )
    attempts: list[ModelDelegateSkillAttemptRecord] = Field(
        default_factory=list,
        description="Best available per-attempt detail in order. Rich local "
        "attempts include the terminal attempt; escalation_history fallback may "
        "contain rejected attempts only. attempts_count remains authoritative.",
    )

    @model_validator(mode="after")
    def validate_structured_terminal_evidence(self) -> Self:
        """Preserve the canonical Core terminal-evidence invariants."""
        required_bar = self.required_quality_bar
        comparison = self.score_vs_required_bar
        if (required_bar is None) != (comparison is None):
            msg = (
                "required_quality_bar and score_vs_required_bar must be "
                "provided together"
            )
            raise ValueError(msg)

        if required_bar is not None and comparison is not None:
            expected = (
                EnumQualityScoreComparison.BELOW_BAR
                if self.quality_score < required_bar
                else EnumQualityScoreComparison.AT_OR_ABOVE_BAR
            )
            if comparison is not expected:
                msg = (
                    "score_vs_required_bar must match quality_score and "
                    "required_quality_bar"
                )
                raise ValueError(msg)

        if self.quality_gate_passed and self.failed_acceptance_criteria:
            msg = "quality_gate_passed response cannot carry failed_acceptance_criteria"
            raise ValueError(msg)
        if self.quality_gate_passed and self.terminal_failure_cause is not None:
            msg = "successful delegation cannot carry terminal_failure_cause"
            raise ValueError(msg)
        return self


__all__ = [
    "ModelDelegateSkillAttemptRecord",
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
]
