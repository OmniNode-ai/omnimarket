# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_automation_liveness — the liveness of every automatic process."""

from omnimarket.nodes.node_projection_automation_liveness.handlers.handler_projection_automation_liveness import (
    HandlerProjectionAutomationLiveness,
)

__all__ = [
    "HandlerProjectionAutomationLiveness",
    "NodeProjectionAutomationLiveness",
]


class NodeProjectionAutomationLiveness(HandlerProjectionAutomationLiveness):
    """ONEX entry-point wrapper for HandlerProjectionAutomationLiveness."""
