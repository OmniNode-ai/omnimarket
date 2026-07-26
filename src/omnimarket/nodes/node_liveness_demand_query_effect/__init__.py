# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_liveness_demand_query_effect — real Postgres demand-source query EFFECT.

OMN-15126 implementation of the OMN-14845 design (design §3.2 steps 1-2 and
§4). Performs the actual I/O: queries a `table_query` demand source for
eligible demand, then for each eligible item queries the declared
`expected_output_join` target for a matching, predicate-satisfying row. The
state DECISION (NOT_READY/NO_DEMAND/HEALTHY/STALE/RED) is made downstream,
by the pure `node_liveness_evaluate_compute` handler, from this node's
already-fetched result.
"""

from __future__ import annotations

from omnimarket.nodes.node_liveness_demand_query_effect.handlers.handler_liveness_demand_query_effect import (
    HandlerLivenessDemandQueryEffect,
)
from omnimarket.nodes.node_liveness_demand_query_effect.models.model_liveness_demand_query_request import (
    ModelLivenessDemandQueryRequest,
)
from omnimarket.nodes.node_liveness_demand_query_effect.models.model_liveness_demand_query_result import (
    ModelLivenessDemandQueryResult,
)


class NodeLivenessDemandQueryEffect(HandlerLivenessDemandQueryEffect):
    """ONEX entry-point wrapper for HandlerLivenessDemandQueryEffect (OMN-15126)."""


__all__ = [
    "HandlerLivenessDemandQueryEffect",
    "ModelLivenessDemandQueryRequest",
    "ModelLivenessDemandQueryResult",
    "NodeLivenessDemandQueryEffect",
]
