# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus-model seam check effect node."""

from .handlers.handler_bus_model_seam_check import (
    HandlerBusModelSeamCheck,
)


class NodeBusModelSeamCheckEffect(HandlerBusModelSeamCheck):
    """ONEX entrypoint for the bus-model seam gate."""


__all__ = ["HandlerBusModelSeamCheck", "NodeBusModelSeamCheckEffect"]
