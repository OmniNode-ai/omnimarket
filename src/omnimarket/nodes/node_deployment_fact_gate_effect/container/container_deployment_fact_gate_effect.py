# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    HandlerDeploymentFactGate,
)


def build_fact_gate() -> HandlerDeploymentFactGate:
    """Build the handler with its constructor defaults."""
    return HandlerDeploymentFactGate()
