# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeExtractionVerifyCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.transcript_extraction import (
    ModelExtractionVerifyRequest,
    ModelExtractionVerifyResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeExtractionVerifyCompute(
    NodeCompute[ModelExtractionVerifyRequest, ModelExtractionVerifyResult]
):
    """Compute node: verifies one segment's proposed extraction events."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeExtractionVerifyCompute"]
