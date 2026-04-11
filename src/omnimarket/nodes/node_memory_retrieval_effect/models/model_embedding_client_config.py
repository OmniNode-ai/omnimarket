# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for the embedding client.

EmbeddingClient.

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelEmbeddingClientConfig",
]


class ModelEmbeddingClientConfig(BaseModel):
    """Configuration for the embedding client.

    Attributes:
        base_url: Base URL of the MLX embedding server. The server should
            expose a POST /embed endpoint. REQUIRED - must be provided explicitly.
        timeout_seconds: Request timeout in seconds. The MLX server typically
            responds in ~1.3ms, so 5 seconds provides ample margin.
        max_retries: Maximum number of retry attempts for transient failures.
            Retries use exponential backoff starting at 0.1 seconds.
        retry_base_delay: Base delay for exponential backoff in seconds.
            Actual delay is: base_delay * (2 ** attempt).
        embedding_dimension: Expected dimension of embedding vectors. Used for
            validation. MLX Qwen3-Embedding produces 1024-dimensional vectors.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str = Field(
        ...,
        description="Base URL of the MLX embedding server (REQUIRED - no default)",
    )
    timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Request timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retry attempts for transient failures",
    )
    retry_base_delay: float = Field(
        default=0.1,
        gt=0,
        description="Base delay for exponential backoff in seconds",
    )
    embedding_dimension: int = Field(
        default=1024,
        gt=0,
        description="Expected embedding vector dimension",
    )
