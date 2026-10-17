# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_sli_compute: the lab's service-level indicators, evaluated (OMN-17427)."""

from omnimarket.nodes.node_lab_sli_compute.handlers.handler_lab_sli_evaluate import (
    HandlerLabSliEvaluate,
)

__all__ = ["HandlerLabSliEvaluate", "NodeLabSliCompute"]


class NodeLabSliCompute(HandlerLabSliEvaluate):
    """ONEX entry-point wrapper for HandlerLabSliEvaluate."""
