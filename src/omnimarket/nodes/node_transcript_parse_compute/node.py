# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeTranscriptParseCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.transcript_parse import (
    ModelTranscriptParseRequest,
    ModelTranscriptParseResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeTranscriptParseCompute(
    NodeCompute[ModelTranscriptParseRequest, ModelTranscriptParseResult]
):
    """Compute node: parses Claude Code transcript JSONL text into typed turns."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeTranscriptParseCompute"]
