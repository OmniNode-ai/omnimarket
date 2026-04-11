# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the mock Qdrant handler.

HandlerQdrantMock.

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ModelHandlerQdrantMockConfig",
]


class ModelHandlerQdrantMockConfig(BaseModel):
    """Configuration for the mock Qdrant handler.

    Attributes:
        embedding_dimension: Dimension of embedding vectors. Defaults to 1024
            (MLX Qwen3-Embedding compatible).
        simulate_latency_ms: Simulated latency for operations in milliseconds.
            Set to 0 for instant responses.
        use_real_embeddings: Whether to use the real MLX embedding server
            instead of mock embeddings. Defaults to False for backwards
            compatibility and test determinism.
        embedding_server_url: URL of the MLX embedding server. REQUIRED when
            use_real_embeddings is True. Must be provided explicitly from
            environment variable OMNIMEMORY__EMBEDDING__SERVER_URL.
        embedding_timeout_seconds: Timeout for embedding requests in seconds.
        embedding_max_retries: Maximum retries for embedding requests.

    Raises:
        ValueError: If use_real_embeddings is True but embedding_server_url
            is not provided or is not a valid HTTP(S) URL.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    embedding_dimension: int = Field(
        default=1024,
        description="Dimension of embedding vectors",
    )
    simulate_latency_ms: int = Field(
        default=0,
        ge=0,
        description="Simulated latency in milliseconds",
    )
    use_real_embeddings: bool = Field(
        default=False,
        description="Use real MLX embedding server instead of mock embeddings",
    )
    embedding_server_url: str | None = Field(
        default=None,
        description=(
            "URL of MLX embedding server - REQUIRED when use_real_embeddings=True"
        ),
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

    @model_validator(mode="after")
    def validate_embedding_url_requirement(self) -> Self:
        """Validate that embedding_server_url is provided when use_real_embeddings is True.

        Cross-field validation ensures configuration consistency at model
        construction time rather than waiting for runtime failures.

        Returns:
            Self: The validated model instance.

        Raises:
            ValueError: If use_real_embeddings is True but embedding_server_url
                is not provided or is not a valid HTTP(S) URL.
        """
        if self.use_real_embeddings:
            if not self.embedding_server_url:
                raise ValueError(
                    "embedding_server_url is required when use_real_embeddings is True"
                )
            if not self.embedding_server_url.startswith(("http://", "https://")):
                raise ValueError(
                    f"embedding_server_url must be a valid HTTP(S) URL, "
                    f"got: {self.embedding_server_url!r}"
                )
        return self
