# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Accumulation state for the knowledge context assembler reducer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_fragment import (
    ModelKnowledgeContextFragment,
)

__all__ = ["ModelKnowledgeContextState"]


class ModelKnowledgeContextState(BaseModel):
    """Mutable accumulation state keyed by correlation_id."""

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
