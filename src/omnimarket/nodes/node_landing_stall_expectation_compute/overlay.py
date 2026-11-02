# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall rule's thresholds, read from this node's overlay (OMN-21880).

``<root>/node_landing_stall_expectation_compute/overlay.yaml`` under the first root of
``ONEX_SKILL_OVERLAY_ROOTS`` that holds one. There is no packaged value: a missing or
partial overlay refuses naming the settings, so a threshold is never a literal here.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_landing_stall_expectation_compute.models.model_landing_stall_open import (
    ModelLandingStallThresholds,
)

NODE_NAME = "node_landing_stall_expectation_compute"


def load_stall_thresholds(
    environ: Mapping[str, str] | None = None,
) -> ModelLandingStallThresholds:
    return load_structured_overlay(NODE_NAME, ModelLandingStallThresholds, environ)


__all__ = ["NODE_NAME", "load_stall_thresholds"]
