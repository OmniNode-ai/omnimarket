# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The label-record handler the runtime constructs with no arguments (OMN-20577).

``HandlerDelegationEvalOrchestrator`` takes its snapshot source by injection. The runtime
builds a routed handler without arguments, so this subclass binds the factory the runtime
needs: one tenant-scoped reader per command, built from the node overlay
``delegation_eval_sources``.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_eval_orchestrator.handlers.handler_delegation_eval_orchestrator import (
    HandlerDelegationEvalOrchestrator,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.sources.source_tenant_event_snapshot import (
    snapshots_for_tenant,
)


class HandlerDelegationEvalOrchestratorRuntime(HandlerDelegationEvalOrchestrator):
    """``HandlerDelegationEvalOrchestrator`` with the runtime's tenant-scoped source bound."""

    def __init__(self, container: object | None = None) -> None:
        super().__init__(snapshot_source_factory=snapshots_for_tenant)


__all__ = ["HandlerDelegationEvalOrchestratorRuntime"]
