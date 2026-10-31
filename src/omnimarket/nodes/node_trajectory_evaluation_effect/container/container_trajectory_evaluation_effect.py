# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the handler bound to node_trajectory_evaluation_effect."""

from __future__ import annotations

from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
    HandlerTrajectoryEvaluation,
)

__all__ = ["create_handler_trajectory_evaluation"]


def create_handler_trajectory_evaluation() -> HandlerTrajectoryEvaluation:
    """Build the node handler with its bound dependencies."""
    return HandlerTrajectoryEvaluation()
