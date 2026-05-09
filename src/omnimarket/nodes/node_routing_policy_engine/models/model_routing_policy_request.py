# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for routing policy selection."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTaskType(StrEnum):
    CODE = "code"
    REASONING = "reasoning"
    EMBEDDING = "embedding"
    SUMMARIZATION = "summarization"
    GENERAL = "general"


class EnumCapabilityRequirement(StrEnum):
    CODE_GENERATION = "code_generation"
    TOOL_USE = "tool_use"
    LONG_CONTEXT = "long_context"
    REASONING = "reasoning"
    FAST_INFERENCE = "fast_inference"


class ModelAvailableModel(BaseModel):
    """A candidate model with its capability score and cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(..., min_length=1, description="Unique model identifier key.")
    score: float = Field(
        ..., ge=0.0, le=1.0, description="Capability score for this task type."
    )
    cost_per_token: float = Field(
        ..., ge=0.0, description="Relative cost per token (lower = cheaper)."
    )
    capabilities: frozenset[EnumCapabilityRequirement] = Field(
        default_factory=frozenset,
        description="Capabilities supported by this model.",
    )


class ModelRoutingPolicyRequest(BaseModel):
    """Input to the routing policy engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: EnumTaskType = Field(..., description="Type of task to route.")
    required_capabilities: frozenset[EnumCapabilityRequirement] = Field(
        default_factory=frozenset,
        description="Capabilities the selected model must support.",
    )
    available_models: tuple[ModelAvailableModel, ...] = Field(
        ..., min_length=1, description="Ordered candidate models with scores."
    )
    max_cost_per_token: float | None = Field(
        default=None,
        ge=0.0,
        description="Hard cost ceiling. Models exceeding it are excluded.",
    )
    # Seeded value in [0, 1) used to implement the exploit/explore split
    # deterministically. Callers supply this so the handler stays pure.
    # None means "no seed provided" — handler falls back to exploit unconditionally.
    exploration_seed: float | None = Field(
        default=None,
        ge=0.0,
        lt=1.0,
        description="Uniform random value in [0, 1) for exploit/explore branching. None = always exploit.",
    )
    # Fraction of traffic sent to explore (minority tier). Default 0.15 = 15%.
    exploration_rate: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Fraction of requests routed to exploration candidate.",
    )
    request_id: str = Field(default="", description="Correlation ID for this request.")


__all__ = [
    "EnumCapabilityRequirement",
    "EnumTaskType",
    "ModelAvailableModel",
    "ModelRoutingPolicyRequest",
]
