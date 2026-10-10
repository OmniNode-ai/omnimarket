# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Product-floor alarm verdict compute node (OMN-18008)."""

from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_product_floor_verdict import (
    HandlerProductFloorVerdict,
)


class NodeProductFloorVerdictCompute(HandlerProductFloorVerdict):
    """ONEX entrypoint for definition-B product-floor alarm decisions."""


__all__ = ["HandlerProductFloorVerdict", "NodeProductFloorVerdictCompute"]
