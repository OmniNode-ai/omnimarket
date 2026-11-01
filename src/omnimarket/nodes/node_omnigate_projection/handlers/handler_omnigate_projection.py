# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_omnigate_projection.

REDUCER node. Wraps the pure ``reduce_omnigate_projection`` function so the
runtime can resolve and invoke it via the standard handler-routing protocol.

The handler takes one ``ModelOmniGateProjectionInput`` (activity rows, metrics
snapshot and the raw event) and returns a ``ModelOmniGateProjectionResult``.
"""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_omnigate_projection.models.model_omnigate_projection_input import (
    ModelOmniGateProjectionInput,
)
from omnimarket.nodes.node_omnigate_projection.models.model_omnigate_projection_result import (
    ModelOmniGateProjectionResult,
)
from omnimarket.nodes.node_omnigate_projection.reducers.reducer_omnigate_projection import (
    reduce_omnigate_projection,
)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]


class HandlerOmniGateProjection:
    """Reducer handler wrapping the pure ``reduce_omnigate_projection`` function.

    The runtime resolves this class via handler_routing in contract.yaml.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle(
        self, request: ModelOmniGateProjectionInput
    ) -> ModelOmniGateProjectionResult:
        """Apply the request's event to its activity rows and metrics snapshot."""
        next_activity, next_metrics = reduce_omnigate_projection(
            tuple(request.activity), request.metrics, request.event
        )
        return ModelOmniGateProjectionResult(
            activity=list(next_activity), metrics=next_metrics
        )


__all__ = ["HandlerOmniGateProjection"]
