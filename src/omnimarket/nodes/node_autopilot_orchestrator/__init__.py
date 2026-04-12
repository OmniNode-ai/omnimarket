# SPDX-License-Identifier: MIT
"""node_autopilot_orchestrator — 4-phase autonomous close-out pipeline orchestrator."""

from omnimarket.nodes.node_autopilot_orchestrator.handlers.handler_autopilot_orchestrator import (
    HandlerAutopilotOrchestrator,
)


class NodeAutopilotOrchestrator(HandlerAutopilotOrchestrator):
    """ONEX entry-point wrapper for HandlerAutopilotOrchestrator."""
