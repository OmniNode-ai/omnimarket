# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared knowledge context event models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EnumBundleStatus",
    "EnumFragmentSource",
    "ModelKnowledgeContextBundle",
    "ModelKnowledgeContextFragment",
    "ModelKnowledgeContextState",
]


class EnumFragmentSource(StrEnum):
    CODEBASE_INTELLIGENCE = "codebase_intelligence"
    ANTIPATTERN_MATCH = "antipattern_match"
    AGENT_LEARNING_RETRIEVAL = "agent_learning_retrieval"
    ARCHITECTURE_GRAPH = "architecture_graph"


class ModelKnowledgeContextFragment(BaseModel):
    """One backend's contribution to the assembled context bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fragment_source: EnumFragmentSource = Field(
        ..., description="Which backend produced this fragment"
    )
    content: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw payload from the backend, empty when error is set",
    )
    correlation_id: str = Field(
        ..., description="Correlation ID from the original request"
    )
    error: str | None = Field(
        default=None,
        description="Error message when the backend failed; None on success",
    )

    @property
    def ok(self) -> bool:
        return self.error is None


class EnumBundleStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    DEGRADED = "DEGRADED"


class ModelKnowledgeContextBundle(BaseModel):
    """Final assembled bundle with all backend fragments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        ..., description="Correlation ID from the original request"
    )
    status: EnumBundleStatus = Field(
        ...,
        description="COMPLETE=all fragments ok, PARTIAL=some failed, DEGRADED=all failed",
    )
    fragments: tuple[ModelKnowledgeContextFragment, ...] = Field(
        default=(),
        description="All collected fragments including error fragments",
    )
    fragment_count: int = Field(..., description="Total number of fragments collected")


class ModelKnowledgeContextState(BaseModel):
    """Accumulation state keyed by correlation_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Request correlation ID")
    expected_count: int = Field(
        ..., description="Number of backends expected to respond"
    )
    fragments: tuple[ModelKnowledgeContextFragment, ...] = Field(
        default=(),
        description="Collected fragments so far",
    )
    completed: bool = Field(
        default=False,
        description="True when len(fragments) >= expected_count",
    )
