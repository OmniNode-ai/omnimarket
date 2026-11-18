# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeLaneGithubReadGuardEffect - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_effect import NodeEffect

from omnimarket.nodes.node_lane_gh_read_guard_effect.handlers.handler_runner_placement_snapshot_policy import (
    HandlerRunnerPlacementSnapshotPolicy,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeLaneGithubReadGuardEffect(NodeEffect):
    """Effect node that refuses a lane's GitHub reads around the gh shim."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["HandlerRunnerPlacementSnapshotPolicy", "NodeLaneGithubReadGuardEffect"]
