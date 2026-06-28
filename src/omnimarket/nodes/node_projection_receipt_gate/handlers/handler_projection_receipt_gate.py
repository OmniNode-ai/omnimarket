# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_projection_receipt_gate.

REDUCER node. Wraps the pure ``reduce_receipt_gate`` function so the
runtime can resolve and invoke it via the standard handler-routing protocol.

The handler accepts a dict envelope:
    {
        "rows": [...],   # list of serialized ModelReceiptGateRow
        "event": {...},  # raw event dict
    }

and returns:
    {
        "rows": [...],   # updated list of serialized ModelReceiptGateRow
    }
"""

from __future__ import annotations

from typing import Any, Literal

from omnimarket.nodes.node_projection_receipt_gate.models.model_receipt_gate_row import (
    ModelReceiptGateRow,
)
from omnimarket.nodes.node_projection_receipt_gate.reducers.reducer_receipt_gate import (
    reduce_receipt_gate,
)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]


class HandlerProjectionReceiptGate:
    """Reducer handler wrapping the pure ``reduce_receipt_gate`` function.

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
            input_data: Dict with keys ``rows`` and ``event``.

        Returns:
            Dict with key ``rows`` (list of row dicts).
        """
        raw_rows: list[dict[str, Any]] = input_data.get("rows") or []
        event: dict[str, Any] = input_data.get("event") or {}

        rows = tuple(ModelReceiptGateRow.model_validate(row) for row in raw_rows)

        next_rows = reduce_receipt_gate(rows, event)

        return {
            "rows": [row.model_dump(mode="json", by_alias=True) for row in next_rows],
        }

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Unified entry point — delegates to handle_dict."""
        return self.handle_dict(input_data)


__all__ = ["HandlerProjectionReceiptGate"]
