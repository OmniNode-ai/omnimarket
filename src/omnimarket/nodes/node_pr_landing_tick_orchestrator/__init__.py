# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing tick orchestrator node: one landing tick through the node path, compared per pull request."""

from pathlib import Path

from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)


class NodePrLandingTickOrchestrator(HandlerPrLandingTickOrchestrator):
    """ONEX entry-point wrapper for HandlerPrLandingTickOrchestrator."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerPrLandingTickOrchestrator", "NodePrLandingTickOrchestrator"]
