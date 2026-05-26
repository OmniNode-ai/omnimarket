# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_demo_cost_compute [OMN-12235].

COMPUTE node — pure, idempotent. Applies per-model pricing tables to
inference results and returns cost per model plus cheapest model ID.

node_not_implemented: true — raise NotImplementedError until Wave 7 implementation.
"""

from __future__ import annotations

from omnimarket.nodes.node_demo_cost_compute.models.model_cost_request import (
    ModelDemoCostRequest,
    ModelDemoCostResult,
)


class NodeDemoCostCompute:
    """COMPUTE — pricing lookup and cost calculation from inference results."""

    def handle(self, request: ModelDemoCostRequest) -> ModelDemoCostResult:
        raise NotImplementedError  # stub-ok
