# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Memory Retrieval Request model for memory search operations.

This module defines the request envelope used by the memory_retrieval_effect node
to perform search operations (semantic, full-text, graph traversal) on memory
snapshots across Qdrant, PostgreSQL, and Graph backends.

Example:
    >>> from omnimemory.nodes.node_memory_retrieval_effect.models import (
    ...     ModelMemoryRetrievalRequest,
    ... )
    >>> # Semantic search with text query (will be embedded)
    >>> request = ModelMemoryRetrievalRequest(
    ...     operation="search",
    ...     query_text="decisions about authentication",
    ...     limit=10,
    ...     similarity_threshold=0.7,
    ... )
    >>> # Semantic search with pre-computed embedding
    >>> request = ModelMemoryRetrievalRequest(
    ...     operation="search",
    ...     query_embedding=[0.1, 0.2, ...],  # 1024-dim vector
    ...     limit=5,
    ... )

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ModelMemoryRetrievalRequest"]


class ModelMemoryRetrievalRequest(BaseModel):
    """Request envelope for memory retrieval operations.

    This model encapsulates all parameters needed to perform search operations
    on memory snapshots. The operation field determines the search strategy,
    and other fields provide search-specific parameters.

    Supported operations:
        - search: Semantic similarity search using vector embeddings
        - search_text: Full-text search using SQL LIKE/FTS
        - search_graph: Graph traversal for relationship-based retrieval

    Attributes:
        operation: The search operation to perform.
        query_text: Text query for search. For 'search' operation, this will
            be embedded if query_embedding is not provided.
        query_embedding: Pre-computed embedding vector. If provided for 'search'
            operation, query_text embedding is skipped.
        snapshot_id: Starting snapshot ID for graph traversal operations.
        limit: Maximum number of results to return. Defaults to 10.
        similarity_threshold: Minimum similarity score (0.0-1.0) for semantic
            search results. Defaults to 0.7.
        traversal_depth: Maximum depth for graph traversal. Defaults to 2.
        relationship_types: Filter graph traversal by relationship types.
        metadata_filters: Key-value filters to apply to search results.
        tags: Filter results by tags.

    Example:
        >>> # Semantic search
        >>> semantic_request = ModelMemoryRetrievalRequest(
        ...     operation="search",
        ...     query_text="user authentication flow",
        ...     limit=5,
        ...     similarity_threshold=0.8,
        ... )
        >>>
        >>> # Full-text search
        >>> text_request = ModelMemoryRetrievalRequest(
        ...     operation="search_text",
        ...     query_text="login error",
        ...     limit=20,
        ... )
        >>>
        >>> # Graph traversal
        >>> graph_request = ModelMemoryRetrievalRequest(
        ...     operation="search_graph",
        ...     snapshot_id="snap_abc123",
        ...     traversal_depth=3,
        ...     relationship_types=["related_to", "caused_by"],
        ... )

    Raises:
        ValueError: If required fields for the operation are missing.
    """

    model_config = ConfigDict(frozen=True)

    operation: Literal["search", "search_text", "search_graph"] = Field(
        ...,
        description="The search operation to perform",
    )

    query_text: str | None = Field(
        default=None,
        description="Text query (required for search/search_text unless embedding)",
    )

    query_embedding: list[float] | None = Field(
        default=None,
        description="Pre-computed embedding vector for semantic search",
    )

    snapshot_id: str | None = Field(
        default=None,
        description="Starting snapshot ID for graph traversal",
    )

    limit: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Maximum number of results to return",
    )

    similarity_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum similarity score for semantic search (0.0-1.0)",
    )

    traversal_depth: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Maximum depth for graph traversal",
    )

    relationship_types: list[str] | None = Field(
        default=None,
        description="Filter graph traversal by relationship types",
    )

    metadata_filters: dict[str, str] | None = Field(
        default=None,
        description="Key-value filters to apply to results",
    )

    tags: list[str] | None = Field(
        default=None,
        description="Filter results by tags",
    )

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        """Validate that required fields are present for each operation type.

        Validation rules:
            - search: query_text OR query_embedding required
            - search_text: query_text required
            - search_graph: snapshot_id required

        Returns:
            Self: The validated instance.

        Raises:
            ValueError: If required fields are missing for the operation.
        """
        if self.operation == "search":
            if self.query_text is None and self.query_embedding is None:
                raise ValueError(
                    "'search' operation requires 'query_text' or 'query_embedding'"
                )
        elif self.operation == "search_text" and self.query_text is None:
            raise ValueError("'search_text' operation requires 'query_text'")
        elif self.operation == "search_graph" and self.snapshot_id is None:
            raise ValueError("'search_graph' operation requires 'snapshot_id'")
        return self
