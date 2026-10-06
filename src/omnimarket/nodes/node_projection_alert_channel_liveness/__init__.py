# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_alert_channel_liveness — durable alert-channel verdicts."""

from omnimarket.nodes.node_projection_alert_channel_liveness.handlers.handler_projection_alert_channel_liveness import (
    HandlerProjectionAlertChannelLiveness,
)

__all__ = [
    "HandlerProjectionAlertChannelLiveness",
    "NodeProjectionAlertChannelLiveness",
]


class NodeProjectionAlertChannelLiveness(HandlerProjectionAlertChannelLiveness):
    """ONEX entry-point wrapper for HandlerProjectionAlertChannelLiveness."""
