# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven retrieval evaluation compute node (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.retrieval_eval import (
    ModelRetrievalEvalReport,
    ModelRetrievalEvalRequest,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeRetrievalEvalCompute(
    NodeCompute[ModelRetrievalEvalRequest, ModelRetrievalEvalReport]
):
    """Compute node: evaluates ranked retrieval results."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeRetrievalEvalCompute"]
