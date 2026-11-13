# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay limits the contract held before they moved into the overlay (OMN-23963)."""

from __future__ import annotations

from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelPublishLimits,
)

TEST_LIMITS: ModelPublishLimits = ModelPublishLimits.model_validate(
    {
        "cycle_bound_seconds": 255,
        "step_timeout_seconds": {
            "hosts": 60,
            "pool": 60,
            "friction": 10,
            "process_health": 90,
            "runner_fleet": 10,
            "join_j2": 90,
            "join_j5": 90,
            "join_j6": 90,
            "join_j7": 90,
            "friction_classed": 90,
            "join_refusal": 30,
            "delegation_tracker": 90,
            "beta_board": 85,
            "mvp_launch_reads": 90,
            "floor_projections": 60,
            "floor_emissions": 60,
            "join_j16": 30,
            "join_j0": 30,
            "factory_floor_unresolved": 90,
            "factory_floor": 45,
            "floor_simple": 15,
            "floor_compare": 15,
            "floor_canary": 20,
        },
    }
)
