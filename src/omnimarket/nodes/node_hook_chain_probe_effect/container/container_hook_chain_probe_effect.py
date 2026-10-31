# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_hook_chain_probe_effect.handlers.handler_hook_chain_probe import (
    HandlerHookChainProbe,
)


def build_handler_hook_chain_probe() -> HandlerHookChainProbe:
    """Build the handler with its constructor defaults."""
    return HandlerHookChainProbe()
