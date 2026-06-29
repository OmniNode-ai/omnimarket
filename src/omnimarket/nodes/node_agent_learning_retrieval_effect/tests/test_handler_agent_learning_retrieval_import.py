# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_agent_learning_retrieval."""

from omnimarket.nodes.node_agent_learning_retrieval_effect.handlers.handler_agent_learning_retrieval import (
    HandlerAgentLearningRetrieval,
)


def test_handler_agent_learning_retrieval_importable() -> None:
    assert HandlerAgentLearningRetrieval is not None
