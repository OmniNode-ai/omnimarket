# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_claim_resolver — resolver-backed agent claim verification."""

from omnimarket.nodes.node_claim_resolver.handlers.handler_claim_resolver import (
    HandlerClaimResolver,
)
from omnimarket.nodes.node_claim_resolver.models import (
    EnumAgentClaimKind,
    EnumClaimResolutionStatus,
    ModelAgentClaim,
    ModelClaimResolutionRequest,
    ModelClaimResolutionResponse,
    ModelClaimResolutionResult,
)

__all__ = [
    "EnumAgentClaimKind",
    "EnumClaimResolutionStatus",
    "HandlerClaimResolver",
    "ModelAgentClaim",
    "ModelClaimResolutionRequest",
    "ModelClaimResolutionResponse",
    "ModelClaimResolutionResult",
    "NodeClaimResolver",
]


class NodeClaimResolver(HandlerClaimResolver):
    """ONEX entry-point wrapper for HandlerClaimResolver."""
