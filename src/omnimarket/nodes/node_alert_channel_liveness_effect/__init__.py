# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_alert_channel_liveness_effect — the channel proves itself (OMN-15600)."""

from omnimarket.nodes.node_alert_channel_liveness_effect.handlers.handler_alert_channel_liveness import (
    HandlerAlertChannelLiveness,
)

__all__ = [
    "HandlerAlertChannelLiveness",
    "NodeAlertChannelLivenessEffect",
]


class NodeAlertChannelLivenessEffect(HandlerAlertChannelLiveness):
    """ONEX entry-point wrapper for HandlerAlertChannelLiveness."""
