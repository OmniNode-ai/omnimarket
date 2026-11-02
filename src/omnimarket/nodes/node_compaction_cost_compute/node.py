# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven compaction cost node; behavior belongs to the handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from .models.model_compaction_cost import (
    ModelCompactionCostRequest,
    ModelCompactionCostResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeCompactionCostCompute(
    NodeCompute[ModelCompactionCostRequest, ModelCompactionCostResult]
):
    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)
