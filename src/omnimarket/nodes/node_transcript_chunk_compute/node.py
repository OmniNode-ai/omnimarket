# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeTranscriptChunkCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.transcript_extraction import (
    ModelTranscriptChunkRequest,
    ModelTranscriptChunkResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeTranscriptChunkCompute(
    NodeCompute[ModelTranscriptChunkRequest, ModelTranscriptChunkResult]
):
    """Compute node for plan task TE.2."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeTranscriptChunkCompute"]
