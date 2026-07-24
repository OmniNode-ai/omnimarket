# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_worker_memory_admission_compute — RAM-aware worker admission (D3, OMN-14977).

Pure COMPUTE node (rule 7a) implementing the headroom formula and fail-closed
staleness admission gate spec'd in
``docs/plans/2026-07-23-distributed-validation-context-aware-runtime-plan.md``
§2 D3: ``usable = total - wired - inference_reservation``, a heartbeat-cadenced
advertisement with a ``2 * cadence_seconds`` staleness bound, and a typed
``refused`` receipt at the WORKER admission point (never a silent queue).

This node is a prerequisite for activating ``.200`` (or any second host) as a
push-validation worker — see plan §4 Phase C ordering.
"""

from omnimarket.nodes.node_worker_memory_admission_compute.handlers.handler_worker_memory_admission_compute import (
    HandlerWorkerMemoryAdmissionCompute,
)


class NodeWorkerMemoryAdmissionCompute(HandlerWorkerMemoryAdmissionCompute):
    """ONEX entry-point wrapper for HandlerWorkerMemoryAdmissionCompute (OMN-14977)."""


__all__ = [
    "HandlerWorkerMemoryAdmissionCompute",
    "NodeWorkerMemoryAdmissionCompute",
]
