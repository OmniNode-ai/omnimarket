# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A hand edit of a standard ticket in Linear raises a typed error and an alarm."""

from __future__ import annotations

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_alarm import (
    HandlerTicketHandEditAlarm,
)

__all__ = ["HandlerTicketHandEditAlarm", "NodeTicketHandEditAlarmEffect"]


class NodeTicketHandEditAlarmEffect(HandlerTicketHandEditAlarm):
    """ONEX entry-point wrapper for HandlerTicketHandEditAlarm."""
