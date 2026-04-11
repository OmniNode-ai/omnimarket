# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Memory Retrieval Response Model.

This module defines the response envelope for memory retrieval operations.
The response wraps search results including retrieved snapshots with their
relevance/similarity scores and error information when operations fail.

Example:
    >>> from omnimemory.nodes.node_memory_retrieval_effect.models import (
    ...     ModelMemoryRetrievalResponse,
    ...     ModelSearchResult,
    ... )
    >>> response = ModelMemoryRetrievalResponse(
    ...     status="success",
    ...     results=[
    ...         ModelSearchResult(snapshot=snap1, score=0.95),
    ...         ModelSearchResult(snapshot=snap2, score=0.87),
    ...     ],
    ...     total_count=2,
    ... )

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
"""

from __future__ import annotations

from typing import Literal

from omnibase_core.models.omnimemory import (
    ModelMemorySnapshot,
)
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelMemoryRetrievalResponse", "ModelSearchResult"]


class ModelSearchResult(BaseModel):
    """A single search result with snapshot and relevance score.

    Attributes:
        snapshot: The retrieved memory snapshot.
        score: Relevance/similarity score (0.0-1.0). For semantic search,
            this is cosine similarity. For text search, this may be a
            normalized relevance score. For graph traversal, this represents
            path weight or distance.
        distance: Optional raw distance metric from vector search.
        path: Optional traversal path for graph search results (list of
            snapshot IDs from start to this result).
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    snapshot: ModelMemorySnapshot = Field(
        ...,
        description="The retrieved memory snapshot",
    )

    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Relevance/similarity score (0.0-1.0)",
    )

    distance: float | None = Field(
        default=None,
        description="Raw distance metric from vector search",
    )

    path: list[str] | None = Field(
        default=None,
        description="Traversal path for graph results (snapshot IDs)",
    )


class ModelMemoryRetrievalResponse(BaseModel):
    """Response envelope for memory retrieval operations.

    This model provides a consistent response structure for all memory retrieval
    operations (search, search_text, search_graph). The status field indicates
    the operation outcome, while results carry the matched snapshots with scores.

    Attributes:
        status: Operation status (success, error, no_results).
        results: List of search results with snapshots and scores, ordered
            by relevance (highest score first).
        total_count: Total number of results returned.
        query_embedding_used: The embedding vector used for semantic search
            (useful for debugging or caching).
        error_message: Detailed error information when status is "error".

    Example:
        >>> # Successful search with results
        >>> response = ModelMemoryRetrievalResponse(
        ...     status="success",
        ...     results=[result1, result2],
        ...     total_count=2,
        ... )
        >>>
        >>> # No results found
        >>> response = ModelMemoryRetrievalResponse(
        ...     status="no_results",
        ...     results=[],
        ...     total_count=0,
        ... )
        >>>
        >>> # Error case
        >>> response = ModelMemoryRetrievalResponse(
        ...     status="error",
        ...     error_message="Connection to vector store failed",
        ... )
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    status: Literal["success", "error", "no_results"] = Field(
        ...,
        description="Operation status: success, error, or no_results",
    )

    results: list[ModelSearchResult] = Field(
        default_factory=list,
        description="Search results ordered by relevance (highest score first)",
    )

    total_count: int = Field(
        default=0,
        ge=0,
        description="Total number of results returned",
    )

    query_embedding_used: list[float] | None = Field(
        default=None,
        description="Embedding vector used for semantic search (for debugging)",
    )

    error_message: str | None = Field(
        default=None,
        description="Error details if status is error",
    )
