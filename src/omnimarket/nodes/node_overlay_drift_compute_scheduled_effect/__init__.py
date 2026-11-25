# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_overlay_drift_compute_scheduled_effect: the overlay consumer join check on the runtime tick (OMN-24990)."""

from omnimarket.nodes.node_overlay_drift_compute_scheduled_effect.handlers.handler_overlay_drift_compute_scheduled import (
    HandlerOverlayDriftComputeScheduled,
)

__all__ = [
    "HandlerOverlayDriftComputeScheduled",
    "NodeOverlayDriftComputeScheduledEffect",
]


class NodeOverlayDriftComputeScheduledEffect(HandlerOverlayDriftComputeScheduled):
    """ONEX entry-point wrapper for HandlerOverlayDriftComputeScheduled."""
