# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared request model for the delegation escalation-decision COMPUTE (OMN-13476).

Lives in the shared ``omnimarket.routing`` package — not inside either node — so
both ``node_delegation_escalation_decision_compute`` (the COMPUTE that owns the
decision) and ``node_delegation_orchestrator`` (the caller that delegates it) can
import it without a cross-node model reach-in (guarded by
``tests/test_no_cross_node_reach_in.py``).

Carries only the pure, already-resolved decision state. The config-dependent
inputs — the next eligible tier and the precise no-higher-tier reason — are
resolved from the routing contract+overlay by the orchestrator (the routing
reducer's ``next_eligible_tier`` / ``describe_no_higher_tier_available`` read
``routing_tiers.yaml`` and the task-class contract) and passed in here as plain
values. The COMPUTE performs ZERO I/O: it only applies the deterministic
escalation precedence rules to these inputs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelEscalationDecisionRequest(BaseModel):
    """Pure, zero-I/O inputs for the escalation/tier decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    escalation_count: int = Field(
        ...,
        ge=0,
        description=(
            "Number of escalations already performed for this workflow. "
            "Compared against ``max_escalation_attempts``."
        ),
    )
    max_escalation_attempts: int = Field(
        ...,
        ge=0,
        description=(
            "Ceiling on escalations for this decision path. The inference-error "
            "path and the quality-gate path tune this independently."
        ),
    )
    current_tier_name: str | None = Field(
        default=None,
        description=(
            "Name of the tier whose attempt just failed. ``None`` means the "
            "current tier could not be identified — escalation cannot proceed."
        ),
    )
    error_retryable: bool = Field(
        default=True,
        description=(
            "Whether the underlying failure is retryable on a higher tier. The "
            "quality-gate path always passes True (a sub-bar score is retryable "
            "by definition). The inference-error path passes the classification "
            "from ``_should_escalate_inference_error`` (False for empty body / "
            "empty choices)."
        ),
    )
    next_tier_name: str | None = Field(
        default=None,
        description=(
            "The next eligible tier resolved by the orchestrator from the "
            "routing contract+overlay. ``None`` means the ladder is exhausted. "
            "This COMPUTE never resolves tiers itself — config resolution is the "
            "orchestrator's I/O boundary."
        ),
    )
    non_retryable_reason: str = Field(
        default="non_retryable_inference_response",
        description=(
            "Terminal reason token to record when ``error_retryable`` is False."
        ),
    )
    no_higher_tier_reason: str | None = Field(
        default=None,
        description=(
            "Precise terminal reason supplied by the orchestrator (from "
            "``describe_no_higher_tier_available`` / the bare "
            "``no_higher_tier_available`` token) for when ``next_tier_name`` is "
            "None. Required when the ladder is exhausted; ignored otherwise."
        ),
    )


__all__ = ["ModelEscalationDecisionRequest"]
