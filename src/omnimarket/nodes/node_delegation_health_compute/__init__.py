# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation-health verdict compute (OMN-21927)."""

from omnimarket.nodes.node_delegation_health_compute.handlers.handler_delegation_health import (
    HandlerDelegationHealth,
)


class NodeDelegationHealthCompute(HandlerDelegationHealth):
    """ONEX entrypoint for the delegation-health verdict."""


__all__ = ["HandlerDelegationHealth", "NodeDelegationHealthCompute"]
