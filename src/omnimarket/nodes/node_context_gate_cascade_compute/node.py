# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point; transport and routing live in contract.yaml."""

from omnimarket.nodes.node_context_gate_cascade_compute.handlers.handler_gate_cascade import (
    HandlerContextGateCascade,
)


class NodeContextGateCascadeCompute(HandlerContextGateCascade):
    """Gate cascade compute node."""
