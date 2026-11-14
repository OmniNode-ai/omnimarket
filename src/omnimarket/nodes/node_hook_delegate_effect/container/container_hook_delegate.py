# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds the hook delegate effect's container and handler."""

from __future__ import annotations

from omnibase_core.container import ModelONEXContainer
from omnibase_core.utils.util_run_coro_sync import run_coro_sync

from omnimarket.nodes.node_hook_delegate_effect.adapters.adapter_delegate_cli import (
    AdapterDelegateCli,
)
from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
    CLI_PROTOCOL,
    HandlerHookDelegate,
)
from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_prompt_delegate import (
    HandlerPromptDelegate,
)


def create_hook_delegate_container(
    cli: object | None = None,
) -> ModelONEXContainer:
    """A container holding the `onex delegate` runner (the real process unless given)."""
    container = ModelONEXContainer()
    run_coro_sync(
        container.service_registry.register_instance(
            interface=CLI_PROTOCOL, instance=cli or AdapterDelegateCli()
        )
    )
    return container


def build_handler(cli: object | None = None) -> HandlerHookDelegate:
    """The handler as the runtime builds it: from its container."""
    return HandlerHookDelegate(create_hook_delegate_container(cli))


def build_prompt_handler(cli: object | None = None) -> HandlerPromptDelegate:
    """The prompt handler (OMN-23413 AC33), built from its container the same way."""
    return HandlerPromptDelegate(create_hook_delegate_container(cli))
