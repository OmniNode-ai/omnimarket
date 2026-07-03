# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tool-reuse match request model (OMN-13356).

A request to evaluate an incoming tool-generation request against the registry
of already-generated tools *before* invoking the LLM generation flow.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_enums import (
    EnumToolReuseMatchStrategy,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_signature import (
    ModelInputOutputSignature,
)


class ModelToolReuseRequest(BaseModel):
    """Request to match an incoming tool request against the generated-tool registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(description="Run correlation ID for event tracing")
    task_description: str = Field(
        min_length=10,
        description="Natural-language description of the requested tool task",
    )
    requested_signature: ModelInputOutputSignature = Field(
        description="Contract signature (input/output models) of the requested tool"
    )
    match_strategy: EnumToolReuseMatchStrategy = Field(
        default=EnumToolReuseMatchStrategy.HYBRID,
        description="Matching algorithm selection",
    )
    similarity_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Minimum lexical similarity (0.0-1.0) for a SEMANTIC/HYBRID match",
    )
    max_candidates: int = Field(
        default=5,
        ge=1,
        description="Maximum candidate tools to return for ranking/disambiguation",
    )


__all__ = ["ModelToolReuseRequest"]
