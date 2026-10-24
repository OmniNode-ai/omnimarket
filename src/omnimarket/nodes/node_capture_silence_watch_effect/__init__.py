# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Timer-driven capture silence watch effect (OMN-21340)."""

from omnimarket.nodes.node_capture_silence_watch_effect.handlers.handler_capture_silence_watch import (
    HandlerCaptureSilenceWatch,
)

__all__ = ["HandlerCaptureSilenceWatch", "NodeCaptureSilenceWatchEffect"]


class NodeCaptureSilenceWatchEffect(HandlerCaptureSilenceWatch):
    """ONEX entry-point wrapper for HandlerCaptureSilenceWatch."""
