# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for the LLM delegation routing compute node (OMN-11775)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_request import (
    ModelLlmDelegationRequest,
)
from omnimarket.models.delegation.llm_cost_routing.model_routing_policy import (
    ModelDelegationRoutingPolicy,
)


class DegradationEntry(BaseModel):
    """Records that a (task_type, model_id) pair is degraded until expires_at."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    expires_at: datetime
    """UTC datetime after which the degradation entry is no longer active."""

    reason: str
    """Human-readable reason recorded when degradation was triggered."""


class HealthEntry(BaseModel):
    """Runtime health snapshot for a single model endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    healthy: bool
    has_capacity: bool
    """False if the model is rate-limited or at capacity."""

    checked_at: datetime
    """UTC datetime of the most recent health check."""


class ModelDelegationRoutingInput(BaseModel):
    """Input to the delegation routing compute node.

    All fields are immutable. The node produces a deterministic ModelSelection
    given the same inputs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    request: ModelLlmDelegationRequest
    policy: ModelDelegationRoutingPolicy

    degradation_state: dict[tuple[str, str], DegradationEntry]
    """Maps (task_type, model_id) → DegradationEntry. Entry is active if expires_at > now."""

    health_state: dict[str, HealthEntry]
    """Maps endpoint_env → HealthEntry."""
