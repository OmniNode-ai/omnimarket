# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router import HandlerModelRouter
from omnimarket.nodes.node_model_router.handlers.handler_model_router import Registry

if TYPE_CHECKING:
    from omnibase_infra.mixins.mixin_llm_http_transport import MixinLlmHttpTransport
    from omnibase_infra.nodes.node_llm_inference_effect import (
        HandlerLlmOpenaiCompatible,
    )

    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerAntigravityArguments,
        HandlerClaudeCodeArguments,
        HandlerClaudeGlmArguments,
        HandlerCodexArguments,
        HandlerDelegationModelCall,
    )


def build_handler_model_router(
    *,
    policy: ModelRoutingPolicy,
    registry: Registry,
    event_bus: Any,
) -> HandlerModelRouter:
    """Build the model router with its policy, registry and event bus."""
    return HandlerModelRouter(policy=policy, registry=registry, event_bus=event_bus)


def build_handler_llm_openai_compatible(
    *,
    transport: MixinLlmHttpTransport,
) -> HandlerLlmOpenaiCompatible:
    """Build the inference handler with its HTTP transport."""
    from omnibase_infra.nodes.node_llm_inference_effect import (
        HandlerLlmOpenaiCompatible,
    )

    return HandlerLlmOpenaiCompatible(transport=transport)


def build_handler_delegation_model_call(
    *,
    router: Any,
    inference: Any,
) -> HandlerDelegationModelCall:
    """Build the model call adapter after its module is loaded."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerDelegationModelCall,
    )

    return HandlerDelegationModelCall(router=router, inference=inference)


def build_handler_codex_arguments() -> HandlerCodexArguments:
    """Build the Codex argument adapter."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerCodexArguments,
    )

    return HandlerCodexArguments()


def build_handler_claude_code_arguments() -> HandlerClaudeCodeArguments:
    """Build the Claude Code argument adapter."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerClaudeCodeArguments,
    )

    return HandlerClaudeCodeArguments()


def build_handler_claude_glm_arguments() -> HandlerClaudeGlmArguments:
    """Build the Claude GLM argument adapter."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerClaudeGlmArguments,
    )

    return HandlerClaudeGlmArguments()


def build_handler_antigravity_arguments() -> HandlerAntigravityArguments:
    """Build the Antigravity argument adapter."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        HandlerAntigravityArguments,
    )

    return HandlerAntigravityArguments()
