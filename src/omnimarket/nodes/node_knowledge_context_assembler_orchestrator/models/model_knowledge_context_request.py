# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for the knowledge context assembler orchestrator."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EnumContextLevel",
    "ModelKnowledgeContextRequest",
]


class EnumContextLevel(StrEnum):
    L2 = "L2"
    L3 = "L3"


class ModelKnowledgeContextRequest(BaseModel):
    """Command to assemble a knowledge context bundle for a given repo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Unique correlation ID for tracing")
    repo: str = Field(..., description="Repository name to assemble context for")
    level: EnumContextLevel = Field(
        default=EnumContextLevel.L2,
        description="Context depth: L2 (codebase+antipattern+learning) or L3 (+ arch graph)",
    )
    task_description: str | None = Field(
        default=None,
        description="Optional task description to narrow retrieval",
    )
