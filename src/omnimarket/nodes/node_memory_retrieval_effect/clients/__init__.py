# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Memory Retrieval Effect clients.

This package contains client implementations for external services used by
the memory_retrieval_effect node.

Clients:
    - EmbeddingClient: Async client for MLX embedding server

Example::

    import asyncio
    import os
    from omnimemory.nodes.node_memory_retrieval_effect.clients import (
        EmbeddingClient,
        ModelEmbeddingClientConfig,
    )

    async def example():
        # URL must be provided explicitly (from environment variable)
        embedding_url = os.environ["OMNIMEMORY__EMBEDDING__SERVER_URL"]
        config = ModelEmbeddingClientConfig(base_url=embedding_url)
        client = EmbeddingClient(config)

        async with client:
            embedding = await client.get_embedding("Hello world")
            print(f"Embedding dimension: {len(embedding)}")

    asyncio.run(example())

.. versionadded:: 0.1.0
    Initial implementation for OMN-1387.
"""

from ..models import ModelEmbeddingClientConfig
from .embedding_client import EmbeddingClient

__all__ = [
    "EmbeddingClient",
    "ModelEmbeddingClientConfig",
]
