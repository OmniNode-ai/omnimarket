# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for the knowledge query federation orchestrator."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["EnumKnowledgeQueryBackend", "ModelKnowledgeQueryFederationRequest"]


class EnumKnowledgeQueryBackend(StrEnum):
    """Knowledge query backends available for federation.

    Attributes:
        MEMGRAPH: Architecture graph queries (dependencies, blast radius, imports).
        REPOWISE: Codebase intelligence queries (specific files, functions, classes).
        QDRANT: Semantic pattern/smell queries (antipatterns, code duplicates).
    """

    MEMGRAPH = "memgraph"
    REPOWISE = "repowise"
    QDRANT = "qdrant"


class ModelKnowledgeQueryFederationRequest(BaseModel):
    """Request envelope for federated knowledge queries.

    Attributes:
        query: The natural-language knowledge query to route and execute.
        force_backends: Optional explicit backend override (skips classifier).
        correlation_id: Request correlation ID for tracing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(
        ..., description="Natural-language knowledge query", min_length=1
    )

    force_backends: list[EnumKnowledgeQueryBackend] | None = Field(
        default=None,
        description="Explicit backend list — bypasses routing classifier when set",
    )

    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Request correlation ID for tracing",
    )
