# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeSkillDispatchEngineOrchestrator — thin router for the dispatch_engine skill.

Capability: skill.dispatch_engine

Routes a dispatch request through the two already-real pieces: RSD scoring
(``node_rsd_fill_compute``) then self-healing per-repo fan-out
(``node_self_healing_dispatch_orchestrator``), returning a real dispatch receipt
with concrete worker specs. Dispatch logic lives in the handlers
(``HandlerDispatchEngineRouter`` / ``HandlerSkillRequested``); this node is a thin
coordination shell (OMN-13834).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_orchestrator import NodeOrchestrator

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeSkillDispatchEngineOrchestrator(NodeOrchestrator):
    """Orchestrator node for the dispatch_engine skill.

    Capability: skill.dispatch_engine

    All behavior defined in ``contract.yaml``. Routing logic lives in
    ``HandlerDispatchEngineRouter``; the skill-lifecycle boundary lives in
    ``HandlerSkillRequested``. This node is a thin coordination shell.
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeSkillDispatchEngineOrchestrator"]
