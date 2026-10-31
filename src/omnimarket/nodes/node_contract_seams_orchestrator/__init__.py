# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_contract_seams_orchestrator: contracts and seams first for a design (OMN-21627)."""

from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_contract_seams_run import (
    HandlerContractSeamsRun,
)

__all__ = ["HandlerContractSeamsRun", "NodeContractSeamsOrchestrator"]


class NodeContractSeamsOrchestrator(HandlerContractSeamsRun):
    """ONEX entry-point wrapper for HandlerContractSeamsRun."""
