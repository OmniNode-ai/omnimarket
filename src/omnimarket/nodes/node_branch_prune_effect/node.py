# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Remote-branch prune effect node, bound to its handler by contract (OMN-21890)."""

from __future__ import annotations

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_branch_prune_effect.handlers.handler_branch_prune import (
    HandlerBranchPrune,
)
from omnimarket.nodes.node_branch_prune_effect.models import (
    ModelBranchPruneRequest,
    ModelBranchPruneResult,
)
from omnimarket.nodes.node_branch_prune_effect.protocols import (
    ProtocolBranchHost,
    ProtocolTipRecord,
)


class NodeBranchPruneEffect:
    """Prune merged and closed remote branches through an injected host."""

    def __init__(self, host: ProtocolBranchHost, tips: ProtocolTipRecord) -> None:
        self.handler = HandlerBranchPrune(host=host, tips=tips)

    def handle(
        self, request: ModelBranchPruneRequest | ModelRuntimeTick
    ) -> ModelBranchPruneResult:
        return self.handler.handle(request)
