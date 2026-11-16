# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_overlay_drift_scheduled_effect: the overlay drift read on the runtime tick (OMN-24271)."""

from omnimarket.nodes.node_overlay_drift_scheduled_effect.handlers.handler_overlay_drift_scheduled import (
    HandlerOverlayDriftScheduled,
)

__all__ = ["HandlerOverlayDriftScheduled", "NodeOverlayDriftScheduledEffect"]


class NodeOverlayDriftScheduledEffect(HandlerOverlayDriftScheduled):
    """ONEX entry-point wrapper for HandlerOverlayDriftScheduled."""
