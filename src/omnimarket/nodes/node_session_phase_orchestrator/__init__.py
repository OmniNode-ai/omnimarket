# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_session_phase_orchestrator — routes phase evaluation results to transition or halt."""

from omnimarket.nodes.node_session_phase_orchestrator.handlers.handler_session_phase_orchestrator import (
    HandlerSessionPhaseOrchestrator,
    ModelPhaseEvaluationResult,
)

__all__ = [
    "HandlerSessionPhaseOrchestrator",
    "ModelPhaseEvaluationResult",
    "NodeSessionPhaseOrchestrator",
]


class NodeSessionPhaseOrchestrator(HandlerSessionPhaseOrchestrator):
    """ONEX entry-point wrapper for HandlerSessionPhaseOrchestrator."""
