# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared swarm-fanout event model — canonical home for a cross-node contract.

``ModelSwarmFanoutResult`` is the ``swarm-fanout-completed.v1`` terminal event
payload, owned by ``node_swarm_fanout_orchestrator`` but also consumed by
``node_swarm_subtask_state_reducer`` (folding fanout completion into per-subtask
FSM state). It must not live inside either node's own package to avoid the
cross-node reach-in ``tests/test_no_cross_node_reach_in.py`` guards against
(OMN-14534 / OMN-9263 precedent).

Note: this module still imports ``ModelSwarmDispatch`` from
``node_swarm_fanout_orchestrator`` — that per-dispatch record stays owned by
the orchestrator node (out of scope for this relocation) and is not itself a
cross-node reach-in target (this module lives outside ``omnimarket.nodes``, so
the reach-in guard's ``nodes/`` rglob does not scan it).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)


class ModelSwarmFanoutResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatches: tuple[ModelSwarmDispatch, ...]
    wall_latency_ms: int
    sum_subtask_latency_ms: int
    run_id: str = ""

    # Terminal event payload fields (swarm-fanout-completed.v1)
    completed_count: int = 0
    failed_count: int = 0
    degraded: bool = False
    aggregation_mode: str = "collect_all"
    endpoint_registry_hash: str = ""
    routing_policy_hash: str = ""
    projection_ref: str = ""
