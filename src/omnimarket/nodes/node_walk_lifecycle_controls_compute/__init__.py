# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lifecycle grading of the call recorder against the observer, with positive controls."""

from omnimarket.nodes.node_walk_lifecycle_controls_compute.handlers.handler_lifecycle_controls import (
    HandlerLifecycleControls,
)


class NodeWalkLifecycleControlsCompute(HandlerLifecycleControls):
    """ONEX entry point."""


__all__ = ["HandlerLifecycleControls", "NodeWalkLifecycleControlsCompute"]
