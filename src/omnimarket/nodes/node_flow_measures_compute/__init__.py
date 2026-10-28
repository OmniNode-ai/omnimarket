# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_flow_measures_compute: the flow measures of the factory tick (OMN-21441)."""

from omnimarket.nodes.node_flow_measures_compute.handlers.handler_flow_measures import (
    HandlerFlowMeasures,
)

__all__ = ["HandlerFlowMeasures", "NodeFlowMeasuresCompute"]


class NodeFlowMeasuresCompute(HandlerFlowMeasures):
    """ONEX entry-point wrapper for HandlerFlowMeasures."""
