# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven node for the compaction receipts read model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_reducer import NodeReducer

from omnimarket.nodes.node_projection_compaction_receipts.models import (
    ModelCompactionReceiptsProjectionRequest,
    ModelCompactionReceiptsProjectionResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeProjectionCompactionReceipts(
    NodeReducer[
        ModelCompactionReceiptsProjectionRequest,
        ModelCompactionReceiptsProjectionResult,
    ]
):
    """The contract routes completed confirmations to the effect-class writer."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeProjectionCompactionReceipts"]
