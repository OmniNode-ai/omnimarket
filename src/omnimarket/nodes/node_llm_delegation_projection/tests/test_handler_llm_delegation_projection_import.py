# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_llm_delegation_projection."""

from omnimarket.nodes.node_llm_delegation_projection.handlers.handler_llm_delegation_projection import (
    HandlerLlmDelegationProjection,
)


def test_handler_llm_delegation_projection_importable() -> None:
    assert HandlerLlmDelegationProjection is not None
