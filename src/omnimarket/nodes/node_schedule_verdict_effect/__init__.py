# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_schedule_verdict_effect: the schedule verdict on the runtime's tick (OMN-19998)."""

from omnimarket.nodes.node_schedule_verdict_effect.handlers.handler_schedule_verdict_scheduled import (
    HandlerScheduleVerdictScheduled,
)

__all__ = ["HandlerScheduleVerdictScheduled", "NodeScheduleVerdictEffect"]


class NodeScheduleVerdictEffect(HandlerScheduleVerdictScheduled):
    """ONEX entry-point wrapper for HandlerScheduleVerdictScheduled."""
