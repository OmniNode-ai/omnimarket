# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_stability_stack_plan_compute: every stack fact the plan folds, none in code.

Found at ``<root>/node_stability_stack_plan_compute/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package
ships ``overlay.example.yaml`` with synthetic values. The overlay models themselves live in
``models/model_stack_plan.py`` beside the request and plan models; this module only loads the overlay, so a deployment's
stack descriptions, reserved lanes and dev-merge tag pattern are data and the handler stays pure. Nothing here has a default:
an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_stability_stack_plan_compute.models.model_stack_plan import (
    ModelStackPlanOverlay,
)

NODE_NAME = "node_stability_stack_plan_compute"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelStackPlanOverlay:
    """Load the node's overlay from the first root in ``ONEX_SKILL_OVERLAY_ROOTS`` holding one."""
    return load_structured_overlay(NODE_NAME, ModelStackPlanOverlay, environ)


__all__ = ["NODE_NAME", "ModelStackPlanOverlay", "load_overlay"]
