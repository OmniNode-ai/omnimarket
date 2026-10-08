# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Database-backed branch claim check effect (OMN-20708)."""

from omnimarket.nodes.node_branch_claim_check_effect.handlers.handler_branch_claim_check import (
    HandlerBranchClaimCheck,
)


class NodeBranchClaimCheckEffect(HandlerBranchClaimCheck):
    """ONEX entry point."""


__all__ = ["HandlerBranchClaimCheck", "NodeBranchClaimCheckEffect"]
