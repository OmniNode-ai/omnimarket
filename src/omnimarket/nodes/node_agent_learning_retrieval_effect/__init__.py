# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Agent learning retrieval effect node — cross-agent memory fabric.

Migrated from omnimemory (OMN-8298, Wave 2).
Qdrant client and retrieval functions remain in omnimemory and are
injected/invoked at runtime via DI. Omnimarket owns the contract,
the models (re-exported from omnimemory), and the entry point.
"""

from omnimarket.nodes.node_agent_learning_retrieval_effect.models import (
    EnumRetrievalMatchType,
    EnumRetrievalTaskType,
    ModelAgentLearningRetrievalRequest,
    ModelAgentLearningRetrievalResponse,
    ModelRetrievedLearning,
)

__all__ = [
    "EnumRetrievalMatchType",
    "EnumRetrievalTaskType",
    "ModelAgentLearningRetrievalRequest",
    "ModelAgentLearningRetrievalResponse",
    "ModelRetrievedLearning",
    "NodeAgentLearningRetrievalEffect",
]


class NodeAgentLearningRetrievalEffect:
    """ONEX entry-point marker for node_agent_learning_retrieval_effect."""

    __onex_node_type__ = "node_agent_learning_retrieval_effect"
