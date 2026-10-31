# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the four handlers the context pack orchestrator composes."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_pack_render_compute.handlers.handler_context_pack_render import (
    HandlerContextPackRender,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.handlers.handler_context_task_kind_rules import (
    HandlerContextTaskKindRules,
)

__all__ = [
    "create_handler_context_pack_builder",
    "create_handler_context_pack_render",
    "create_handler_context_profile_resolver",
    "create_handler_context_task_kind_rules",
    "resolve_parts",
]


def create_handler_context_profile_resolver(
    profiles_dir: Path | None = None,
) -> HandlerContextProfileResolver:
    """Bind the resolver to its profile directory."""
    return HandlerContextProfileResolver(profiles_dir)


def create_handler_context_pack_builder() -> HandlerContextPackBuilder:
    """Build the stateless pack builder."""
    return HandlerContextPackBuilder()


def create_handler_context_pack_render() -> HandlerContextPackRender:
    """Build the stateless pack renderer."""
    return HandlerContextPackRender()


def create_handler_context_task_kind_rules() -> HandlerContextTaskKindRules:
    """Build the stateless task kind evaluator."""
    return HandlerContextTaskKindRules()


def resolve_parts(
    profile_resolver: HandlerContextProfileResolver | None,
    builder: HandlerContextPackBuilder | None,
    renderer: HandlerContextPackRender | None,
    task_kind_rules: HandlerContextTaskKindRules | None,
) -> tuple[
    HandlerContextProfileResolver,
    HandlerContextPackBuilder,
    HandlerContextPackRender,
    HandlerContextTaskKindRules,
]:
    """Keep each injected handler; bind the default for each one left out."""
    return (
        profile_resolver
        if profile_resolver is not None
        else create_handler_context_profile_resolver(),
        builder if builder is not None else create_handler_context_pack_builder(),
        renderer if renderer is not None else create_handler_context_pack_render(),
        task_kind_rules
        if task_kind_rules is not None
        else create_handler_context_task_kind_rules(),
    )
