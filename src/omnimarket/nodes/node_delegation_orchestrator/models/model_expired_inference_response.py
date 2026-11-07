# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed evidence for an inference response that arrived past its run's bound (OMN-22822).

On the dev lane on 2026-11-06 and 2026-11-07 the dead-letter replay node
re-delivered inference responses from 2026-11-03 to 2026-11-05 onto the live
inference-response topic. A run still ``ROUTED`` on the attempt such a response
named took it as a fresh answer: a failure retried or escalated to the next
rung and called a provider again, days after the caller had stopped waiting.
In the four hours to 2026-11-07T03:00Z, 2,093 runs created on 2026-11-04 to
2026-11-06 were moved again, 1,880 of them onto the cheap_frontier rung (the
free OpenRouter model), which spent its daily cap.

A response that arrives after the run's contract-declared completion bound
(``completion_bound.max_wall_seconds``) is no longer an answer anyone can use.
The orchestrator records it with this model on
``DelegationWorkflowState.expired_inference_response``, closes the run FAILED
with a timeout outcome, and opens no new attempt.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelExpiredInferenceResponse(BaseModel):
    """Evidence record for one inference response refused as expired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Workflow correlation the expired response addressed."
    )
    attempt_id: UUID | None = Field(
        ...,
        description="Inference-attempt identity the expired response carried.",
    )
    response_model_used: str = Field(
        ..., description="model_used the expired response reported."
    )
    response_was_error: bool = Field(
        ...,
        description="Whether the expired response was an error. An expired error is the case that used to retry or escalate.",
    )
    elapsed_seconds: int = Field(
        ...,
        ge=0,
        description="Wall seconds from the run's start epoch to the arrival of the response.",
    )
    completion_bound_seconds: int = Field(
        ...,
        gt=0,
        description="The contract-declared completion bound the elapsed time exceeded.",
    )
    current_tier_name: str | None = Field(
        default=None, description="Tier the run was on when the response arrived."
    )
    expired_at: str = Field(
        ..., description="ISO-8601 UTC time the orchestrator refused the response."
    )


__all__: list[str] = ["ModelExpiredInferenceResponse"]
