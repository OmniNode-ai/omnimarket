# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContextCompactionReceiptsFoldCompute - contract-driven; behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.context_compaction_fold import (
    ModelCompactionFoldRequest,
    ModelCompactionFoldResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeContextCompactionReceiptsFoldCompute(
    NodeCompute[ModelCompactionFoldRequest, ModelCompactionFoldResult]
):
    """Compute node: folds compaction receipts without depending on arrival order."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeContextCompactionReceiptsFoldCompute"]
