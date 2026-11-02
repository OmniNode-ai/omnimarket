# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeExtractionRungHarnessCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.extraction_rung import (
    ModelRungHarnessRequest,
    ModelRungHarnessResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeExtractionRungHarnessCompute(
    NodeCompute[ModelRungHarnessRequest, ModelRungHarnessResult]
):
    """Compute node: replays one rung's recorded extraction calls."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeExtractionRungHarnessCompute"]
