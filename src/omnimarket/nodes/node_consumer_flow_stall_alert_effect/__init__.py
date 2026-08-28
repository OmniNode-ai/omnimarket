# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_consumer_flow_stall_alert_effect — the half that speaks (OMN-16778)."""

from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers.handler_consumer_flow_stall_alert import (
    HandlerConsumerFlowStallAlert,
)

__all__ = [
    "HandlerConsumerFlowStallAlert",
    "NodeConsumerFlowStallAlertEffect",
]


class NodeConsumerFlowStallAlertEffect(HandlerConsumerFlowStallAlert):
    """ONEX entry-point wrapper for HandlerConsumerFlowStallAlert."""
