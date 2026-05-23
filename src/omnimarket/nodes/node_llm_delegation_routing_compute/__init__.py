# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""LLM delegation routing compute node — pure model selection, no I/O."""

from omnimarket.nodes.node_llm_delegation_routing_compute.handlers.handler_delegation_routing import (
    HandlerDelegationRouting,
)

__all__ = [
    "HandlerDelegationRouting",
    "NodeLlmDelegationRoutingCompute",
]


class NodeLlmDelegationRoutingCompute(HandlerDelegationRouting):
    """ONEX entry-point wrapper for HandlerDelegationRouting."""
