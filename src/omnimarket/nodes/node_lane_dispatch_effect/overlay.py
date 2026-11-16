# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the deployment binding; an absent overlay refuses dispatch."""

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from .models.model_lane_dispatch_effect import ModelLaneDispatchOverlay


def load_dispatch_overlay() -> ModelLaneDispatchOverlay:
    return load_structured_overlay("lane_dispatch", ModelLaneDispatchOverlay)
