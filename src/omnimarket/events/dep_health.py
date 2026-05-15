# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared dep-health event payload models for cross-node consumption.

Projection consumers import from here instead of reaching into
node_dependency_health_sweep's private models package directly.
"""

from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_completed_event import (
    ModelDepHealthSweepCompletedEvent,
)

__all__ = [
    "ModelDepHealthSweepCompletedEvent",
]
