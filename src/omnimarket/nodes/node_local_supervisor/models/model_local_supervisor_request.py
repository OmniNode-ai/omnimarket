# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelLocalSupervisorRequest — input to HandlerLocalSupervisor.handle()."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRetryStrategy(StrEnum):
    IMPROVED_CONTEXT_RETRY = "improved_context_retry"
    SAME_MODEL_SAME_CONTEXT = "same_model_same_context"
    PEER_MODEL = "peer_model"
    TIER_ESCALATION = "tier_escalation"


class ModelRoutingDecision(BaseModel):
    """A pre-made routing decision — produced by node_routing_policy_engine or equivalent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(..., description="Registry model_id key to invoke.")
    endpoint_url: str = Field(
        ..., description="Resolved base URL for the model endpoint."
    )
    role: str = Field(..., description="Caller role (used for fallback authorization).")
    used_fallback: bool = Field(
        default=False, description="True if the fallback model was selected."
    )


class ModelLocalSupervisorRequest(BaseModel):
    """Request for the local supervisor to execute a routing decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    routing_decision: ModelRoutingDecision = Field(
        ..., description="Pre-made routing decision specifying which model to invoke."
    )
    prompt: str = Field(..., description="Prompt text to send to the selected model.")
    retry_budget: int = Field(
        ...,
        ge=1,
        description=(
            "Maximum number of execution attempts. "
            "Two-Strike protocol: budget >= 3 always escalates on third failure."
        ),
    )
    retry_strategy: EnumRetryStrategy = Field(
        ..., description="Retry strategy to apply on verifier failure."
    )
    correlation_id: str = Field(..., description="Trace/correlation ID.")


__all__: list[str] = [
    "EnumRetryStrategy",
    "ModelLocalSupervisorRequest",
    "ModelRoutingDecision",
]
