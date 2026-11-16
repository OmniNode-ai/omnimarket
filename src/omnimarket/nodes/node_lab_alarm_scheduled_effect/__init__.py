# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_alarm_scheduled_effect: the lab alarm's broker-lag run on the runtime tick (OMN-24029)."""

from omnimarket.nodes.node_lab_alarm_scheduled_effect.handlers.handler_lab_alarm_scheduled import (
    HandlerLabAlarmScheduled,
)

__all__ = ["HandlerLabAlarmScheduled", "NodeLabAlarmScheduledEffect"]


class NodeLabAlarmScheduledEffect(HandlerLabAlarmScheduled):
    """ONEX entry-point wrapper for HandlerLabAlarmScheduled."""
