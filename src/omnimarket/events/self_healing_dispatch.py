# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared re-export of the self-healing dispatch I/O models for cross-node consumption.

Consumers (e.g. node_skill_dispatch_engine_orchestrator's router) import these from
here instead of reaching into node_self_healing_dispatch_orchestrator's private models
package directly. Mirrors the established omnimarket.events.dep_health pattern and keeps
the cross-node reach-in guard (tests/test_no_cross_node_reach_in.py) satisfied without
growing its allowlist. The canonical definitions still live in the owning node
(referenced by its contract input_model); full physical promotion is deferred (OMN-13834).
"""

from __future__ import annotations

from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_request import (
    ModelSelfHealingDispatchRequest,
)
from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_result import (
    EnumDispatchRunStatus,
    EnumWorkerStatus,
    ModelDispatchGroup,
    ModelEscalationRecord,
    ModelSelfHealingDispatchResult,
    ModelStallRecoveryEvent,
    ModelWorkerRecord,
)

__all__ = [
    "EnumDispatchRunStatus",
    "EnumWorkerStatus",
    "ModelDispatchGroup",
    "ModelEscalationRecord",
    "ModelSelfHealingDispatchRequest",
    "ModelSelfHealingDispatchResult",
    "ModelStallRecoveryEvent",
    "ModelWorkerRecord",
]
