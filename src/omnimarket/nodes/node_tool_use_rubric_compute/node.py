# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeToolUseRubricCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_tool_use_rubric_compute.models.model_tool_use_rubric import (
    ModelToolUseRubricRequest,
    ModelToolUseRubricResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeToolUseRubricCompute(
    NodeCompute[ModelToolUseRubricRequest, ModelToolUseRubricResult]
):
    """Compute node: Decide the tool-use rubric scorer plan, the verdict summary and the schema capture cache key from caller-supplied facts."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeToolUseRubricCompute"]
