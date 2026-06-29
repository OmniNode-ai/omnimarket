# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tool-reuse matcher result models (OMN-13356)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_generated_tool import (
    ModelGeneratedToolRecord,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_enums import (
    EnumToolReuseMatchStrategy,
    EnumToolReuseVerdict,
)


class ModelToolReuseCandidate(BaseModel):
    """A registry record scored against one request.

    Confidence and reason are computed per-request, so they live here rather
    than on the immutable ``ModelGeneratedToolRecord``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: ModelGeneratedToolRecord = Field(description="The matched registry record")
    match_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Match score: 1.0 for an exact signature match, lexical similarity otherwise",
    )
    match_reason: str = Field(
        description="Human-readable explanation of why this tool matched"
    )


class ModelToolReuseMatchResult(BaseModel):
    """Result of tool-reuse matching — the verdict plus the resolved tool, if any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(description="Echoed request correlation ID")
    verdict: EnumToolReuseVerdict = Field(description="Reuse decision")
    matched_tool: ModelToolReuseCandidate | None = Field(
        default=None,
        description="Present iff verdict == MATCHED; absent for NO_MATCH / AMBIGUOUS / failure",
    )
    candidate_tools: list[ModelToolReuseCandidate] = Field(
        default_factory=list,
        description="Ranked candidates (highest confidence first); drives AMBIGUOUS disambiguation",
    )
    match_strategy_used: EnumToolReuseMatchStrategy = Field(
        description="Strategy actually used to produce this result"
    )
    failure_reason: str | None = Field(
        default=None,
        description="Set iff verdict == REGISTRY_UNAVAILABLE",
    )


__all__ = ["ModelToolReuseCandidate", "ModelToolReuseMatchResult"]
