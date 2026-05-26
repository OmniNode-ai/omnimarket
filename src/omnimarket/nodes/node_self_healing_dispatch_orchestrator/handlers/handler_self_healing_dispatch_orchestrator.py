# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler stub for node_self_healing_dispatch_orchestrator [OMN-12208].

ORCHESTRATOR node. Consumes ModelSelfHealingDispatchRequest, orchestrates
self-healing ticket dispatch: resolve tickets (or decompose epic) → group by
repo → dispatch workers via TeamCreate → monitor stalls via agent_healthcheck
→ auto-recover stalls (bounded retry) → escalate exhausted tickets to Blocked.

Implementation is deferred (node_not_implemented: true). This stub raises
NotImplementedError so the runtime fails loudly rather than silently misbehaving.
"""

from __future__ import annotations

from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_request import (
    ModelSelfHealingDispatchRequest,
)
from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_result import (
    ModelSelfHealingDispatchResult,
)


class HandlerSelfHealingDispatchOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelSelfHealingDispatchRequest
    ) -> ModelSelfHealingDispatchResult:
        raise NotImplementedError(  # stub-ok
            "node_self_healing_dispatch_orchestrator is not yet implemented (OMN-12208). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
