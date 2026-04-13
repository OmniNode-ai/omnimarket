# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for node_code_embedding_effect.

Schema must match the dict emitted by HandlerCodeEmbeddingEffect.handle().
[OMN-5657, OMN-5665]
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCodeEmbeddingResult(BaseModel):
    """Result of a code entity embedding batch.

    Published to onex.evt.omniintelligence.code-embedded.v1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(
        ..., min_length=1, description="Propagated from handler input"
    )
    embedded_count: int = Field(
        ..., ge=0, description="Number of entities successfully embedded"
    )
    failed_count: int = Field(
        ..., ge=0, description="Number of entities that failed embedding"
    )
    vector_ids: list[str] = Field(
        default_factory=list,
        description="Entity UUIDs of successfully upserted Qdrant points",
    )
    qdrant_collection: str | None = Field(
        default=None,
        description="Qdrant collection name used for this batch",
    )
    batch_size_used: int | None = Field(
        default=None,
        ge=1,
        description="Effective batch size used for this run",
    )


__all__ = ["ModelCodeEmbeddingResult"]
