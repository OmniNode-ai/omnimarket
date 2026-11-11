# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public setup facade over the model connection owner."""

from pathlib import Path

from omnimarket.nodes.node_model_setup_effect.handlers.handler_endpoint_connections import (
    HandlerEndpointConnections,
)
from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
    ModelEndpointConnection,
    ModelEndpointConnectionRequest,
    ModelEndpointConnectionResult,
)


class HandlerModelConnections:
    def __init__(self, *, db_path: Path | None = None) -> None:
        self._handler = HandlerEndpointConnections(db_path=db_path)

    def handle(
        self, request: ModelEndpointConnectionRequest
    ) -> ModelEndpointConnectionResult:
        return self._handler.handle(request)


__all__ = [
    "HandlerModelConnections",
    "ModelEndpointConnection",
    "ModelEndpointConnectionRequest",
    "ModelEndpointConnectionResult",
]
