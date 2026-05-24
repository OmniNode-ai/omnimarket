# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Assembled knowledge context bundle produced by the reducer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_fragment import (
    ModelKnowledgeContextFragment,
)

__all__ = [
    "EnumBundleStatus",
    "ModelKnowledgeContextBundle",
]


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
        description="All collected fragments (including error fragments)",
    )
    fragment_count: int = Field(..., description="Total number of fragments collected")
