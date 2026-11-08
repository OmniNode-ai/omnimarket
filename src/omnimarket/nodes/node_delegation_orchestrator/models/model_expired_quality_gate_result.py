# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed evidence for a quality-gate result that arrived past its run's deadline (OMN-22822).

A quality-gate result carries no time of its own, so its deadline is the run's:
the contract-declared completion bound (``completion_bound.max_wall_seconds``)
after the run started. A dead-letter replay of a failing verdict that reaches a
run still waiting on its gate later than that used to send the run to the next
rung, which calls a provider again for a caller that stopped waiting. The
orchestrator records the result with this model on
``DelegationWorkflowState.expired_quality_gate_result``, closes the run FAILED
with a timeout outcome, and neither re-evaluates nor escalates it.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelExpiredQualityGateResult(BaseModel):
    """Evidence record for one quality-gate result refused as expired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Workflow correlation the expired result addressed."
    )
    result_passed: bool = Field(
        ...,
        description="Whether the expired result passed. An expired failure is the case that used to escalate.",
    )
    result_fail_category: str = Field(
        ..., description="fail_category the expired result reported."
    )
    elapsed_seconds: int = Field(
        ...,
        ge=0,
        description="Wall seconds from the run's start epoch to the arrival of the result.",
    )
    completion_bound_seconds: int = Field(
        ...,
        gt=0,
        description="The contract-declared completion bound the elapsed time exceeded.",
    )
    current_tier_name: str | None = Field(
        default=None, description="Tier the run was on when the result arrived."
    )
    expired_at: str = Field(
        ..., description="ISO-8601 UTC time the orchestrator refused the result."
    )


__all__: list[str] = ["ModelExpiredQualityGateResult"]
