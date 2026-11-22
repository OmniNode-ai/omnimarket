# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_stall_responder_schedule_effect: the stall responder's trigger on the runtime tick."""

from omnimarket.nodes.node_stall_responder_schedule_effect.handlers.handler_stall_responder_schedule import (
    HandlerStallResponderSchedule,
)

__all__ = ["HandlerStallResponderSchedule", "NodeStallResponderScheduleEffect"]


class NodeStallResponderScheduleEffect(HandlerStallResponderSchedule):
    """ONEX entry-point wrapper for HandlerStallResponderSchedule."""
