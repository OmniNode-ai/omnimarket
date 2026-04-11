# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the production Qdrant handler.

HandlerQdrant.

.. versionadded:: 0.1.0
    Initial implementation for OMN-4474.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ModelHandlerQdrantConfig",
]


class ModelHandlerQdrantConfig(BaseModel):
    """Configuration for the production Qdrant handler.

    Attributes:
        qdrant_host: Hostname for the Qdrant server. Defaults to localhost.
        qdrant_port: Port for the Qdrant server. Must be between 1 and 65535.
        collection_name: Name of the Qdrant collection to use.
        vector_size: Dimension of embedding vectors. Must match the embedding
            model (Qwen3-Embedding-8B produces 4096-dim vectors).
        embedding_server_url: URL of the embedding server. Required field;
            must be a valid HTTP(S) URL.
        embedding_timeout_seconds: Timeout for embedding requests in seconds.
        embedding_max_retries: Maximum retries for embedding requests.
        max_chunk_chars: Maximum characters per text chunk before splitting.
            2000 chars approximates 500 tokens for most tokenizers.
        qdrant_timeout_seconds: Timeout for Qdrant operations in seconds.

    Raises:
        ValueError: If embedding_server_url is missing or not a valid HTTP(S) URL.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    qdrant_host: str = Field(
        ...,
        description="Hostname for the Qdrant server",
    )
    qdrant_port: int = Field(
        default=6333,
        ge=1,
        le=65535,
        description="Port for the Qdrant server",
    )
    collection_name: str = Field(
        default="omnimemory_documents",
        description="Name of the Qdrant collection to use",
    )
    vector_size: int = Field(
        default=4096,
        gt=0,
        description="Dimension of embedding vectors — must match embedding model output",
    )
    embedding_server_url: str = Field(
        description="URL of the embedding server — must be a valid HTTP(S) URL",
    )
    embedding_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Timeout for embedding requests in seconds",
    )
    embedding_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Maximum retries for embedding requests",
    )
    max_chunk_chars: int = Field(
        default=2000,
        gt=0,
        description="Maximum characters per text chunk (2000 chars ≈ 500 tokens)",
    )
    qdrant_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Timeout for Qdrant operations in seconds",
    )

    @model_validator(mode="after")
    def validate_embedding_server_url(self) -> Self:
        """Validate that embedding_server_url is a valid HTTP(S) URL.

        Returns:
            Self: The validated model instance.

        Raises:
            ValueError: If embedding_server_url is not a valid HTTP(S) URL.
        """
        if not self.embedding_server_url.startswith(("http://", "https://")):
            raise ValueError(
                f"embedding_server_url must be a valid HTTP(S) URL, "
                f"got: {self.embedding_server_url!r}"
            )
        return self
