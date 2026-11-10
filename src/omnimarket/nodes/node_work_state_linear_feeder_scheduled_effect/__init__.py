# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_state_linear_feeder_scheduled_effect: writes the newest Linear snapshot where the projection reads its tickets on the runtime's schedule (OMN-23427)."""

from omnimarket.nodes.node_work_state_linear_feeder_scheduled_effect.handlers.handler_linear_feeder_scheduled import (
    HandlerWorkStateLinearFeederScheduled,
)

__all__ = [
    "HandlerWorkStateLinearFeederScheduled",
    "NodeWorkStateLinearFeederScheduledEffect",
]


class NodeWorkStateLinearFeederScheduledEffect(HandlerWorkStateLinearFeederScheduled):
    """ONEX entry-point wrapper for HandlerWorkStateLinearFeederScheduled."""
