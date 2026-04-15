# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persona retrieval effect node — read latest persona snapshot.

Migrated from omnimemory (OMN-8298, Wave 2).
Adapters (Postgres persona retrieval) remain in omnimemory and are
injected at runtime via DI. Omnimarket owns the contract, the models,
and the entry point.
"""

from omnimarket.nodes.node_persona_retrieval_effect.models import (
    ModelPersonaRetrievalRequest,
    ModelPersonaRetrievalResponse,
)

__all__ = [
    "ModelPersonaRetrievalRequest",
    "ModelPersonaRetrievalResponse",
    "NodePersonaRetrievalEffect",
]


class NodePersonaRetrievalEffect:
    """ONEX entry-point marker for node_persona_retrieval_effect."""

    __onex_node_type__ = "node_persona_retrieval_effect"
