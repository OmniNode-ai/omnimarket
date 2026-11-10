# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_state_readings_feeder_scheduled_effect: writes the newest reading per key where the projection reads its readings on the runtime's schedule (OMN-23427)."""

from omnimarket.nodes.node_work_state_readings_feeder_scheduled_effect.handlers.handler_readings_feeder_scheduled import (
    HandlerWorkStateReadingsFeederScheduled,
)

__all__ = [
    "HandlerWorkStateReadingsFeederScheduled",
    "NodeWorkStateReadingsFeederScheduledEffect",
]


class NodeWorkStateReadingsFeederScheduledEffect(
    HandlerWorkStateReadingsFeederScheduled
):
    """ONEX entry-point wrapper for HandlerWorkStateReadingsFeederScheduled."""
