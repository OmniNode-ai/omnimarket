# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_verified_dispatch_orchestrator — paired worker/verifier dispatch with bounded escalation."""

from omnimarket.nodes.node_verified_dispatch_orchestrator.handlers.handler_verified_dispatch_orchestrator import (
    HandlerVerifiedDispatchOrchestrator,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_dispatch_request import (
    ModelDispatchRequest,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_escalation_policy import (
    ModelEscalationPolicy,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_verification_bundle import (
    ModelVerificationBundle,
)


class NodeVerifiedDispatchOrchestrator:
    """ONEX entry-point marker for node_verified_dispatch_orchestrator."""

    __onex_node_type__ = "node_verified_dispatch_orchestrator"


__all__ = [
    "HandlerVerifiedDispatchOrchestrator",
    "ModelDispatchRequest",
    "ModelEscalationPolicy",
    "ModelVerificationBundle",
    "NodeVerifiedDispatchOrchestrator",
]
