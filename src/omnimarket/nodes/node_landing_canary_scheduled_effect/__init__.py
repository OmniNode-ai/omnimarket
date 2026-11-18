# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_landing_canary_scheduled_effect: the daily landing canary on the runtime tick (OMN-24117)."""

from omnimarket.nodes.node_landing_canary_scheduled_effect.handlers.handler_landing_canary_scheduled import (
    HandlerLandingCanaryScheduled,
)

__all__ = ["HandlerLandingCanaryScheduled", "NodeLandingCanaryScheduledEffect"]


class NodeLandingCanaryScheduledEffect(HandlerLandingCanaryScheduled):
    """ONEX entry-point wrapper for HandlerLandingCanaryScheduled."""
