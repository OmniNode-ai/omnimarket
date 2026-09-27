# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Evidence record for a single tier attempt during delegation escalation (OMN-12255)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason


class ModelDelegationEscalationAttempt(BaseModel):
    """Evidence record for a single tier attempt during escalation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_name: str = Field(
        ..., description="Name of the tier attempted (e.g. 'local', 'cheap_cloud')."
    )
    model_used: str = Field(..., description="Model identifier used for this attempt.")
    quality_score: float = Field(
        ..., description="Quality gate score for this attempt (0.0-1.0)."
    )
    required_bar: float | None = Field(
        default=None,
        description="Required adequacy bar resolved from task-class/workflow/request authority.",
    )
    actual_score: float | None = Field(
        default=None,
        description="Actual score compared against required_bar for escalation.",
    )
    authority_source: str | None = Field(
        default=None,
        description="Authority source that supplied required_bar.",
    )
    score_source: str | None = Field(
        default=None,
        description="Scoring authority that produced actual_score.",
    )
    failure_reasons: tuple[str, ...] = Field(
        default=(),
        description="Failure reason strings emitted by the quality gate.",
    )
    # OMN-13535: per-attempt served usage + measured metered cost. On a metered
    # tier that is ATTEMPTED-but-rejected (quality gate fails) and escalates to a
    # cheaper/free tier, the metered call still ran and incurred real tokens/cost.
    # Recording them here keeps each attempt's spend auditable and lets the
    # terminal report the cumulative metered cost across all attempted tiers,
    # instead of dropping the rejected metered tier's cost (the terminal otherwise
    # reflects only the final accepted tier — free → cost_usd=0).
    prompt_tokens: int = Field(
        default=0,
        ge=0,
        description="Served prompt tokens this attempt's inference call reported.",
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        description="Served completion tokens this attempt's inference call reported.",
    )
    cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Measured metered cost (USD) for this attempt's served tokens.",
    )
    latency_ms: int = Field(
        ..., description="End-to-end latency for this attempt in milliseconds."
    )
    fallback_recommended: bool = Field(
        ...,
        description="Whether the quality gate recommended fallback to a higher tier.",
    )
    attempted_at: datetime = Field(
        ..., description="Timestamp of the gate evaluation for this attempt."
    )
    routing_decision_id: UUID | None = Field(
        default=None,
        description="ID of the ModelRoutingDecision that produced this attempt, for cross-event correlation.",
    )
    # OMN-16932: the accept/climb verdict for this rung, as a TYPED pair rather
    # than prose. The orchestrator has always made this decision and never
    # recorded it, so an escalation past a working free rung was only inferable
    # from a later provider call showing up in a log — which is how a $0 local
    # answer came to be abandoned three times in favour of two metered 429s
    # without anything in the event log saying so. Required, because an attempt
    # row that cannot say why it was abandoned is the exact record that failed.
    acceptance_decision: EnumDelegationAcceptanceDecision = Field(
        ...,
        description="Whether this rung's response was accepted or the ladder climbed past it.",
    )
    acceptance_reason: EnumDelegationAcceptanceReason = Field(
        ...,
        description="Typed reason for the accept/climb decision on this rung.",
    )
    # OMN-19436: how the provider stopped, whether the output budget cut it
    # off, and which reasoning-preamble rule found the seam. The bus effect
    # already refuses a ``finish_reason=length`` response and the gate already
    # records both facts on its own verdict, but no rung carried them, so the
    # number of truncated or scratchpad-led runs could not be counted from the
    # record. All three default to "not observed" so a workflow state persisted
    # before these fields existed still decodes.
    finish_reason: EnumProviderFinishReason | None = Field(
        default=None,
        description=(
            "How the provider said generation stopped, as this rung observed "
            "it. None when the rung produced no response at all (a failed "
            "call). 'absent' when a response arrived but no stop reason "
            "reached this record, which is the bus path's success case today."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Whether the output-token budget cut this rung's response short. "
            "Derived from finish_reason and refused when it disagrees."
        ),
    )
    reasoning_preamble_rule: str | None = Field(
        default=None,
        description=(
            "Which declared rule separated a leaked reasoning preamble from "
            "the answer on this rung (OMN-18379), as the quality gate reported "
            "it. None when no gate judged this rung."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def derive_truncated_from_the_stop_reason(cls, data: Any) -> Any:
        """Fill ``truncated`` from ``finish_reason`` when the producer omitted it."""
        if not isinstance(data, dict) or "truncated" in data:
            return data
        reason = data.get("finish_reason")
        return {
            **data,
            "truncated": reason is not None
            and str(getattr(reason, "value", reason))
            == EnumProviderFinishReason.LENGTH,
        }

    @model_validator(mode="after")
    def refuse_a_flag_that_contradicts_the_stop_reason(self) -> Self:
        """A truncation flag set beside the stop reason must agree with it."""
        expected = self.finish_reason is EnumProviderFinishReason.LENGTH
        if self.truncated != expected:
            reason = self.finish_reason.value if self.finish_reason else None
            msg = (
                f"truncated={self.truncated} contradicts finish_reason={reason}: "
                "only a 'length' stop reason is a truncation"
            )
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelDelegationEscalationAttempt"]
