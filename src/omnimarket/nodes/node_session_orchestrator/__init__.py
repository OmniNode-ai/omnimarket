# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_session_orchestrator — Unified session orchestrator WorkflowPackage.

All three phases fully implemented (OMN-8367 / OMN-8687):
- Phase 1 (health gate): probes 8 health dimensions, applies blocking rules.
- Phase 2 (RSD scoring): queries Linear, scores tickets, writes rsd-scored snapshot.
- Phase 3 (dispatch): writes in_flight.yaml, dispatches tickets via claude -p subprocesses.
"""

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    EnumSessionStatus,
    HandlerSessionOrchestrator,
    ModelSessionOrchestratorCommand,
    ModelSessionOrchestratorResult,
)


class NodeSessionOrchestrator(HandlerSessionOrchestrator):
    """ONEX entry-point wrapper for HandlerSessionOrchestrator."""


__all__ = [
    "EnumSessionStatus",
    "HandlerSessionOrchestrator",
    "ModelSessionOrchestratorCommand",
    "ModelSessionOrchestratorResult",
    "NodeSessionOrchestrator",
]
