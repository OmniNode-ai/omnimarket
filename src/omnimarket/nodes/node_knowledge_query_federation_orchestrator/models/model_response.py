# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response models for the knowledge query federation orchestrator."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .model_request import EnumKnowledgeQueryBackend

__all__ = ["ModelKnowledgeFederatedResult", "ModelKnowledgeQueryFederationResponse"]


class ModelKnowledgeFederatedResult(BaseModel):
    """A single federated result with provenance.

    Attributes:
        content: The result text from the backend.
        source: Which backend produced this result.
        content_hash: SHA-256 of content — used for cross-backend deduplication.
        rank: Relative rank within the merged result set (lower = more relevant).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(..., description="Result text from the backend")

    source: EnumKnowledgeQueryBackend = Field(
        ..., description="Backend that produced this result"
    )

    rank: int = Field(
        default=0,
        ge=0,
        description="Relative rank within merged results (lower = more relevant)",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """SHA-256 hex digest of the content for deduplication."""
        return hashlib.sha256(self.content.encode()).hexdigest()


class ModelKnowledgeQueryFederationResponse(BaseModel):
    """Response envelope for a federated knowledge query.

    Attributes:
        query: The original query string.
        results: Merged, deduplicated, ranked results with provenance tags.
        backends_queried: Which backends were actually queried.
        error_message: Set when the overall federation failed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(..., description="The original query string")

    results: tuple[ModelKnowledgeFederatedResult, ...] = Field(
        default=(),
        description="Merged deduplicated results with provenance",
    )

    backends_queried: tuple[EnumKnowledgeQueryBackend, ...] = Field(
        default=(),
        description="Backends that were queried",
    )

    error_message: str | None = Field(
        default=None,
        description="Set when federation failed",
    )
