# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_state_alarm_effect: the lab-state alarm with a delivery receipt, and the canary verdict (OMN-19998)."""

from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_lab_state_alarm import (
    HandlerLabStateAlarm,
)

__all__ = ["HandlerLabStateAlarm", "NodeLabStateAlarmEffect"]


class NodeLabStateAlarmEffect(HandlerLabStateAlarm):
    """ONEX entry-point wrapper for HandlerLabStateAlarm."""
