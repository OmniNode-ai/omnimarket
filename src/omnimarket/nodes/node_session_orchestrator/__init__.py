# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_session_orchestrator — Unified session orchestrator WorkflowPackage.

OMN-8367 PoC: Phase 1 (health gate) implemented. Phases 2 and 3 are stubs.
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
