# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the landing goal and repository scope of the landing nodes from the node's overlay."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_goal_overlay import (
    ModelPrLandingGoalOverlay,
)

NODE_NAME = "node_pr_landing_orchestrator"


def load_landing_goal_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelPrLandingGoalOverlay:
    """Read ``<root>/node_pr_landing_orchestrator/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``.

    The package ships ``overlay.example.yaml`` with synthetic values. No key has a default: an absent
    ``landing_goal`` or ``repo_scope`` refuses the load, and an invalid file names its fields.
    """
    return load_structured_overlay(NODE_NAME, ModelPrLandingGoalOverlay, environ)


__all__ = ["NODE_NAME", "load_landing_goal_overlay"]
