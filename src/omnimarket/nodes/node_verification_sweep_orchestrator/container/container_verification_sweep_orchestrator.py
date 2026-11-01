# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the handler bound to node_verification_sweep_orchestrator."""

from __future__ import annotations

from omnimarket.nodes.node_verification_sweep_orchestrator.handlers.handler_verification_sweep_orchestrator import (
    HandlerVerificationSweepOrchestrator,
)

__all__ = ["create_handler_verification_sweep_orchestrator"]


def create_handler_verification_sweep_orchestrator() -> (
    HandlerVerificationSweepOrchestrator
):
    """Build the node handler with its bound dependencies."""
    return HandlerVerificationSweepOrchestrator()
