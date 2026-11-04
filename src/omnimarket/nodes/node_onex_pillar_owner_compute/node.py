# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeOnexPillarOwnerCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_onex_pillar_owner_compute.models.model_onex_pillar_owner import (
    ModelOnexPillarOwnerRequest,
    ModelOnexPillarOwnerResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeOnexPillarOwnerCompute(
    NodeCompute[ModelOnexPillarOwnerRequest, ModelOnexPillarOwnerResult]
):
    """Compute node: Decide the pillar owner command and cache environment from caller-supplied resolver and executable facts."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeOnexPillarOwnerCompute"]
