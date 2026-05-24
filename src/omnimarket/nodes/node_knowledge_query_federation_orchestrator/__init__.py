# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Knowledge Query Federation Orchestrator — ONEX Node.

Routes knowledge queries deterministically to Memgraph, Repowise, and/or Qdrant
based on keyword heuristics, then merges results with provenance tags and
deduplicates by content hash.

Node Type: ORCHESTRATOR
- Keyword-based routing classifier (no LLM dependency)
- Fan-out to all three backends for ambiguous queries
- Content-hash deduplication across backend results
- Provenance tags on every result (source: memgraph|repowise|qdrant)

Models::

    from omnimarket.nodes.node_knowledge_query_federation_orchestrator import (
        EnumKnowledgeQueryBackend,
        ModelKnowledgeFederatedResult,
        ModelKnowledgeQueryFederationRequest,
        ModelKnowledgeQueryFederationResponse,
    )
"""

from .models import (
    EnumKnowledgeQueryBackend,
    ModelKnowledgeFederatedResult,
    ModelKnowledgeQueryFederationRequest,
    ModelKnowledgeQueryFederationResponse,
)

__all__ = [
    "EnumKnowledgeQueryBackend",
    "ModelKnowledgeFederatedResult",
    "ModelKnowledgeQueryFederationRequest",
    "ModelKnowledgeQueryFederationResponse",
]
