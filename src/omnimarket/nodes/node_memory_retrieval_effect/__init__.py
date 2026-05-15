# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Memory retrieval effect node — semantic/text/graph search.

Migrated from omnimemory (OMN-8298, Wave 2).
Adapters (Qdrant, DB, graph) remain in omnimemory and are injected at
runtime via DI. Omnimarket owns the contract, clients, and entry point;
memory DTO imports are compatibility shims to canonical omnimemory models.
"""

from omnimarket.nodes.node_memory_retrieval_effect.models import (
    ModelHandlerMemoryRetrievalConfig,
    ModelMemoryRetrievalRequest,
    ModelMemoryRetrievalResponse,
    ModelSearchResult,
)

__all__ = [
    "ModelHandlerMemoryRetrievalConfig",
    "ModelMemoryRetrievalRequest",
    "ModelMemoryRetrievalResponse",
    "ModelSearchResult",
    "NodeMemoryRetrievalEffect",
]


class NodeMemoryRetrievalEffect:
    """ONEX entry-point marker for node_memory_retrieval_effect."""

    __onex_node_type__ = "node_memory_retrieval_effect"
