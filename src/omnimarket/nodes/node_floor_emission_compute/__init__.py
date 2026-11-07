# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_emission_compute: golden and error receipts per piece and join (OMN-19998)."""

from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)

__all__ = ["HandlerFloorReceipt", "NodeFloorEmissionCompute"]


class NodeFloorEmissionCompute(HandlerFloorReceipt):
    """ONEX entry-point wrapper for HandlerFloorReceipt."""
