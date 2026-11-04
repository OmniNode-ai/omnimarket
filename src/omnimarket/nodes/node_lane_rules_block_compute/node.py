# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeLaneRulesBlockCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_lane_rules_block_compute.models.model_lane_rules_block import (
    ModelLaneRulesBlockRequest,
    ModelLaneRulesBlockResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeLaneRulesBlockCompute(
    NodeCompute[ModelLaneRulesBlockRequest, ModelLaneRulesBlockResult]
):
    """Compute node: Render lane rules and contract instructions or decide the canonical block path from caller-supplied facts."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeLaneRulesBlockCompute"]
