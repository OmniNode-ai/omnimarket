# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Context-pack budget and admission for delegation (OMN-21119, plan TA.18 AC1/AC3)."""

from omnimarket.nodes.node_delegation_context_pack_budget_compute.handlers.handler_delegation_context_pack_budget import (
    HandlerDelegationContextPackBudget,
)


class NodeDelegationContextPackBudgetCompute(HandlerDelegationContextPackBudget):
    """ONEX entrypoint for the delegation orchestrator's context-pack budget."""


__all__ = [
    "HandlerDelegationContextPackBudget",
    "NodeDelegationContextPackBudgetCompute",
]
