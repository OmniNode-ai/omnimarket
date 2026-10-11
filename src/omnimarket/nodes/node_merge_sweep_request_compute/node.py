# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative compute node: routing and output publication belong to its contract."""

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.merge_sweep.model_merge_sweep_run import (
    ModelMergeSweepRunRequest,
)
from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire


class NodeMergeSweepRequestCompute(
    NodeCompute[ModelScheduledFire, ModelMergeSweepRunRequest]
):
    """Build the typed bus request for one scheduled sweep."""
