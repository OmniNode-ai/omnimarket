# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly stop drill: a lane sent above a stop on a test admission instance must be refused."""

from omnimarket.nodes.node_stop_drill_effect.handlers.handler_stop_drill import (
    HandlerStopDrill,
)


class NodeStopDrillEffect(HandlerStopDrill):
    """ONEX entry point."""


__all__ = ["HandlerStopDrill", "NodeStopDrillEffect"]
