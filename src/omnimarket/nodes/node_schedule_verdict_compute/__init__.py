# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_schedule_verdict_compute: the verdict of every declared schedule (OMN-19998)."""

from omnimarket.nodes.node_schedule_verdict_compute.handlers.handler_schedule_verdict import (
    HandlerScheduleVerdict,
)

__all__ = ["HandlerScheduleVerdict", "NodeScheduleVerdictCompute"]


class NodeScheduleVerdictCompute(HandlerScheduleVerdict):
    """ONEX entry-point wrapper for HandlerScheduleVerdict."""
