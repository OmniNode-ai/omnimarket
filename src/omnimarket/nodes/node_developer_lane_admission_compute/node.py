# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeDeveloperLaneAdmissionCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_developer_lane_admission_compute.models import (
    ModelDeveloperLaneAdmissionRequest,
    ModelDeveloperLaneAdmissionResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeDeveloperLaneAdmissionCompute(
    NodeCompute[ModelDeveloperLaneAdmissionRequest, ModelDeveloperLaneAdmissionResult]
):
    """Compute node: the developer's selected lane, and whether the developer may use it."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeDeveloperLaneAdmissionCompute"]
