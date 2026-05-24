# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_delegation_routing."""

from omnimarket.nodes.node_llm_delegation_routing_compute.handlers.handler_delegation_routing import (
    HandlerDelegationRouting,
)


def test_handler_delegation_routing_importable() -> None:
    assert HandlerDelegationRouting is not None
