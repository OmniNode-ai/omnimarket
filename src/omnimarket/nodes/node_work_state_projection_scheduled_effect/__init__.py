# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_state_projection_scheduled_effect: the work-state projection on the runtime's schedule (OMN-22935)."""

from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)

__all__ = [
    "HandlerWorkStateProjectionScheduled",
    "NodeWorkStateProjectionScheduledEffect",
]


class NodeWorkStateProjectionScheduledEffect(HandlerWorkStateProjectionScheduled):
    """ONEX entry-point wrapper for HandlerWorkStateProjectionScheduled."""
