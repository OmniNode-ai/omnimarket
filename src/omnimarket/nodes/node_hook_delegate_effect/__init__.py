# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hook_delegate_effect - answers a delegated Claude Code prompt or subagent task."""

from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
    gateway_owns_session,
)

__all__ = ["gateway_owns_session"]
