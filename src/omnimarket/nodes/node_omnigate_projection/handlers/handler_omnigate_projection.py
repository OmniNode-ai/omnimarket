# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_omnigate_projection.

REDUCER node. Wraps the pure ``reduce_omnigate_projection`` function so the
runtime can resolve and invoke it via the standard handler-routing protocol.

The handler accepts a dict envelope:
    {
        "activity": [...],   # list of serialized ModelOmniGateProjectionRow
        "metrics": {...},    # serialized ModelOmniGateMetricsSnapshot
        "event": {...},      # raw event dict
    }

and returns:
    {
        "activity": [...],
        "metrics": {...},
    }
"""

from __future__ import annotations

from typing import Any, Literal

from omnimarket.nodes.node_omnigate_projection.models.model_omnigate_projection_row import (
    ModelOmniGateMetricsSnapshot,
    ModelOmniGateProjectionRow,
)
from omnimarket.nodes.node_omnigate_projection.reducers.reducer_omnigate_projection import (
    reduce_omnigate_projection,
)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]


class HandlerOmniGateProjection:
    """Reducer handler wrapping the pure ``reduce_omnigate_projection`` function.

    The runtime resolves this class via handler_routing in contract.yaml.
    ``handle_dict`` is the RuntimeLocal-protocol shim used by onex run-node.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle_dict(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Deserialises the incoming envelope, delegates to the pure reducer,
        and re-serialises the result.

        Args:
            input_data: Dict with keys ``activity``, ``metrics``, and ``event``.

        Returns:
            Dict with keys ``activity`` (list of row dicts) and ``metrics`` (dict).
        """
        raw_activity: list[dict[str, Any]] = input_data.get("activity") or []
        raw_metrics: dict[str, Any] = input_data.get("metrics") or {}
        event: dict[str, Any] = input_data.get("event") or {}

        activity = tuple(ModelOmniGateProjectionRow(**row) for row in raw_activity)
        metrics = ModelOmniGateMetricsSnapshot(**raw_metrics)

        next_activity, next_metrics = reduce_omnigate_projection(
            activity, metrics, event
        )

        return {
            "activity": [row.model_dump(mode="json") for row in next_activity],
            "metrics": next_metrics.model_dump(mode="json"),
        }

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Unified entry point — delegates to handle_dict."""
        return self.handle_dict(input_data)


__all__ = ["HandlerOmniGateProjection"]
