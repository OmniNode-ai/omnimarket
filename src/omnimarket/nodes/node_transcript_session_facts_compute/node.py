# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeTranscriptSessionFactsCompute - contract-driven compute node."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.work_classification import ModelSessionFacts
from omnimarket.nodes.node_transcript_session_facts_compute.models import (
    ModelSessionFactsRequest,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeTranscriptSessionFactsCompute(
    NodeCompute[ModelSessionFactsRequest, ModelSessionFacts]
):
    """Compute node: aggregates facts from transcript JSONL text."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeTranscriptSessionFactsCompute"]
