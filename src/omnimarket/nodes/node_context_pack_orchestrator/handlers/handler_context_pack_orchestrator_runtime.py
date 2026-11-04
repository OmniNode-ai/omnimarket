# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime composition root for the context pack orchestrator node.

The runtime builds the contract-named handler with no arguments, so this binds
the contract's context_tier_sources through static imports, leaving
HandlerContextPackOrchestrator() itself unwired.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping

from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.models.context_pack_orchestrator import (
    ModelContextPackBuilt,
    ModelContextPackCommand,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.additive_answer import (
    answer_additive,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.enabled_additive_profiles import (
    load_enabled_additive_profiles,
)
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
    """Bind contract-declared sources and the enabled additive profiles when the runtime builds it.

    ``sources`` and ``enabled_additive_profiles`` default to the contract's own; they are
    arguments only so a test can bind fixtures through this same class.
    """

    def __init__(
        self,
        sources: Mapping[EnumContextFactor, ProtocolContextTierSource] | None = None,
        *,
        enabled_additive_profiles: Collection[str] | None = None,
        implementation_identity: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        super().__init__(
            load_tier_sources(resolve=resolve_tier_source)
            if sources is None
            else sources,
            implementation_identity=implementation_identity,
        )
        self._enabled_additive_profiles = frozenset(
            load_enabled_additive_profiles()
            if enabled_additive_profiles is None
            else enabled_additive_profiles
        )

    def handle(self, command: ModelContextPackCommand) -> ModelContextPackBuilt:
        if command.request.binding_kind is not EnumContextBindingKind.ADDITIVE:
            return super().handle(command)
        return answer_additive(
            command, enabled=self._enabled_additive_profiles, build=super().handle
        )
