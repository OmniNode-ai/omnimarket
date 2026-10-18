# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative Bash tool-result compute; behaviour lives in the handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_tool_result_reduce_compute.models.model_tool_result_reduce import (
    ModelToolResultReduceRequest,
    ModelToolResultReduceResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeToolResultReduceCompute(
    NodeCompute[ModelToolResultReduceRequest, ModelToolResultReduceResult]
):
    """Compute node: preserve Bash command status and diagnostics."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeToolResultReduceCompute"]
