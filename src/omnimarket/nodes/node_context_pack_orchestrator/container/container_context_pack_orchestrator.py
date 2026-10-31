# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed construction of the context pack orchestrator and its handlers."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_pack_orchestrator.container.container_parts import (
    create_handler_context_pack_builder,
    create_handler_context_pack_render,
    create_handler_context_profile_resolver,
    create_handler_context_task_kind_rules,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_context_pack_render_compute.handlers.handler_context_pack_render import (
    RENDER_CONTRACT_VERSION,
    HandlerContextPackRender,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.handlers.handler_context_task_kind_rules import (
    HandlerContextTaskKindRules,
)


def create_handler_context_pack_orchestrator(
    sources: Mapping[EnumContextFactor, ProtocolContextTierSource] | None = None,
    *,
    profile_resolver: HandlerContextProfileResolver | None = None,
    builder: HandlerContextPackBuilder | None = None,
    renderer: HandlerContextPackRender | None = None,
    task_kind_rules: HandlerContextTaskKindRules | None = None,
    implementation_identity: Callable[[], Mapping[str, str]] | None = None,
    render_contract_version: str = RENDER_CONTRACT_VERSION,
    clock: Callable[[], float] | None = None,
) -> HandlerContextPackOrchestrator:
    """Wire explicit dependencies or their default bindings."""
    return HandlerContextPackOrchestrator(
        sources,
        profile_resolver=(
            profile_resolver
            if profile_resolver is not None
            else create_handler_context_profile_resolver()
        ),
        builder=builder
        if builder is not None
        else create_handler_context_pack_builder(),
        renderer=renderer
        if renderer is not None
        else create_handler_context_pack_render(),
        task_kind_rules=(
            task_kind_rules
            if task_kind_rules is not None
            else create_handler_context_task_kind_rules()
        ),
        implementation_identity=implementation_identity,
        render_contract_version=render_contract_version,
        clock=clock,
    )


__all__ = [
    "create_handler_context_pack_builder",
    "create_handler_context_pack_orchestrator",
    "create_handler_context_pack_render",
    "create_handler_context_profile_resolver",
    "create_handler_context_task_kind_rules",
]
