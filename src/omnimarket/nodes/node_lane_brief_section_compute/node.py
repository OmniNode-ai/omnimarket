# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeLaneBriefSectionCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.lane_brief_section import (
    ModelLaneBriefSectionRequest,
    ModelLaneBriefSectionResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeLaneBriefSectionCompute(
    NodeCompute[ModelLaneBriefSectionRequest, ModelLaneBriefSectionResult]
):
    """Compute node: renders section 3a of a lane brief from the lane_brief_section profile's factors."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeLaneBriefSectionCompute"]
