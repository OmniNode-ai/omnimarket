# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical entry point for the goal closure orchestrator (OMN-20033)."""

from omnimarket.nodes.node_goal_verification_orchestrator.handlers.handler_goal_verification import (
    HandlerGoalClosureDelegationTerminal,
    HandlerGoalClosureResult,
    HandlerGoalDisposition,
    HandlerGoalVerdictObserved,
    HandlerGoalVerification,
    HandlerGoalVerificationSweep,
    HandlerUnverifiedCloses,
)

__all__ = [
    "HandlerGoalClosureDelegationTerminal",
    "HandlerGoalClosureResult",
    "HandlerGoalDisposition",
    "HandlerGoalVerdictObserved",
    "HandlerGoalVerification",
    "HandlerGoalVerificationSweep",
    "HandlerUnverifiedCloses",
    "NodeGoalVerificationOrchestrator",
]


class NodeGoalVerificationOrchestrator(HandlerGoalClosureResult):
    """ONEX entrypoint: a closed goal starts one verification run per contract revision."""
