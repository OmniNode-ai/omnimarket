# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_state_alarm_scheduled_effect: the lab state alarm on the runtime tick (OMN-19998)."""

from omnimarket.nodes.node_lab_state_alarm_scheduled_effect.handlers.handler_lab_state_alarm_scheduled import (
    HandlerLabStateAlarmScheduled,
)

__all__ = ["HandlerLabStateAlarmScheduled", "NodeLabStateAlarmScheduledEffect"]


class NodeLabStateAlarmScheduledEffect(HandlerLabStateAlarmScheduled):
    """ONEX entry-point wrapper for HandlerLabStateAlarmScheduled."""
