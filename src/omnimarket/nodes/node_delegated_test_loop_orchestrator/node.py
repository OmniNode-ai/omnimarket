# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative shell for the delegated test loop's definition-B handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeDelegatedTestLoopOrchestrator(NodeOrchestrator):
    """Route through contract.yaml; the handler owns the loop and injected ports."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeDelegatedTestLoopOrchestrator"]
