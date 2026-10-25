# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime composition root for the context pack orchestrator node.

The runtime builds the contract-named handler with no arguments, so this binds
the contract's context_tier_sources through static imports, leaving
HandlerContextPackOrchestrator() itself unwired.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.tier_source_binding import (
    load_tier_sources,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_source_guidance_effect.handlers.handler_context_source_guidance import (
    HandlerContextSourceGuidance,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.handler_context_source_ledger import (
    HandlerContextSourceLedger,
)
from omnimarket.nodes.node_context_source_repo_doctrine_effect.handlers.handler_context_source_repo_doctrine import (
    HandlerContextSourceRepoDoctrine,
)
from omnimarket.nodes.node_context_source_work_state_effect.handlers.handler_context_source_work_state import (
    HandlerContextSourceWorkState,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.handler_session_context_sources import (
    HandlerSessionContextSources,
)

_TIER_SOURCE_FACTORIES: Mapping[str, Callable[[], ProtocolContextTierSource]] = {
    "node_context_source_ledger_effect": HandlerContextSourceLedger,
    "node_context_source_work_state_effect": HandlerContextSourceWorkState,
    "node_session_context_sources_effect": HandlerSessionContextSources,
    "node_context_source_guidance_effect": HandlerContextSourceGuidance,
    "node_context_source_repo_doctrine_effect": HandlerContextSourceRepoDoctrine,
}


def resolve_tier_source(node_name: str) -> ProtocolContextTierSource:
    """Construct the statically registered source for a contract node name."""
    if node_name not in _TIER_SOURCE_FACTORIES:
        raise ValueError(f"No runtime tier source for {node_name!r}")
    return _TIER_SOURCE_FACTORIES[node_name]()


class HandlerContextPackOrchestratorRuntime(HandlerContextPackOrchestrator):
    """Bind contract-declared sources when the runtime constructs the handler."""

    def __init__(self) -> None:
        super().__init__(load_tier_sources(resolve=resolve_tier_source))
