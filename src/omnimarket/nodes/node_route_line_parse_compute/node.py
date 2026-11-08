# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeRouteLineParseCompute - contract-driven compute node (OMN-22263)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_route_line_parse_compute.models import (
    ModelRouteLineParseRequest,
    ModelRouteLineParseResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeRouteLineParseCompute(
    NodeCompute[ModelRouteLineParseRequest, ModelRouteLineParseResult]
):
    """The node; its logic lives in the handlers its contract names."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeRouteLineParseCompute"]
