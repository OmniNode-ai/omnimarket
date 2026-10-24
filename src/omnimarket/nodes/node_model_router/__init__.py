# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_model_router — contract-driven LLM routing substrate."""

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)

__all__ = ["HandlerModelRouter", "ModelRoutingRequest", "NodeModelRouter"]


class NodeModelRouter:
    """ONEX entry-point marker for node_model_router."""

    __onex_node_type__ = "node_model_router"
