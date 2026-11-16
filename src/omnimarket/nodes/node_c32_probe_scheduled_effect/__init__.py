# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_c32_probe_scheduled_effect: the C32 auth-boundary probe on the runtime tick (OMN-24271)."""

from omnimarket.nodes.node_c32_probe_scheduled_effect.handlers.handler_c32_probe_scheduled import (
    HandlerC32ProbeScheduled,
)

__all__ = ["HandlerC32ProbeScheduled", "NodeC32ProbeScheduledEffect"]


class NodeC32ProbeScheduledEffect(HandlerC32ProbeScheduled):
    """ONEX entry-point wrapper for HandlerC32ProbeScheduled."""
