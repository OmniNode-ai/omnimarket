# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_liveness_evaluate_compute — pure demand-aware liveness state decision.

OMN-15126 implementation of the OMN-14845 design (design §3.2). Given the
already-fetched result of node_liveness_demand_query_effect (or an explicit
registry-resolution/demand-query failure), makes ONLY the state decision --
NOT_READY / NO_DEMAND / HEALTHY / STALE / RED -- and emits a
`ModelLivenessReceipt` (omnibase_core). Stateless and deterministic: no I/O,
no network, no clock (the caller supplies `evaluated_at`).
"""

from __future__ import annotations

from omnimarket.nodes.node_liveness_evaluate_compute.handlers.handler_liveness_evaluate_compute import (
    HandlerLivenessEvaluateCompute,
)
from omnimarket.nodes.node_liveness_evaluate_compute.models.model_liveness_evaluate_request import (
    ModelLivenessEvaluateRequest,
)


class NodeLivenessEvaluateCompute(HandlerLivenessEvaluateCompute):
    """ONEX entry-point wrapper for HandlerLivenessEvaluateCompute (OMN-15126)."""


__all__ = [
    "HandlerLivenessEvaluateCompute",
    "ModelLivenessEvaluateRequest",
    "NodeLivenessEvaluateCompute",
]
