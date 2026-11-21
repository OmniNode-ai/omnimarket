# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_stall_responder_scheduled_effect: the stall responder on the runtime tick."""

from omnimarket.nodes.node_stall_responder_scheduled_effect.handlers.handler_stall_responder_scheduled import (
    HandlerStallResponderScheduled,
)

__all__ = ["HandlerStallResponderScheduled", "NodeStallResponderScheduledEffect"]


class NodeStallResponderScheduledEffect(HandlerStallResponderScheduled):
    """ONEX entry-point wrapper for HandlerStallResponderScheduled."""
