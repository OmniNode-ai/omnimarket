# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeWorkClassifyCompute - contract-driven compute node (OMN-22263)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.work_classification.model_work_classification import (
    ModelWorkClassification,
)
from omnimarket.nodes.node_work_classify_compute.models import ModelWorkClassifyRequest

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeWorkClassifyCompute(
    NodeCompute[ModelWorkClassifyRequest, ModelWorkClassification]
):
    """The node; its logic lives in the handlers its contract names."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeWorkClassifyCompute"]
