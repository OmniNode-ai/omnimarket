# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bit-11 auxiliary guard for a configured gateway; never invoke inference."""

from omnimarket.handlers.claude_gateway_settings import (
    check_gateway_hook_binding,
    gateway_hook_enabled,
)
from omnimarket.models.delegation.model_claude_gateway_hook import (
    ModelClaudeGatewayHookRequest,
    ModelClaudeGatewayHookResult,
)


class HandlerClaudeGatewayHook:
    """Honor the existing enforcer switch, then verify the binding's live state."""

    def handle(
        self, request: ModelClaudeGatewayHookRequest
    ) -> ModelClaudeGatewayHookResult:
        try:
            if not gateway_hook_enabled():
                return ModelClaudeGatewayHookResult(decision="allow")
            return check_gateway_hook_binding(request.cwd)
        except (OSError, ValueError):
            return ModelClaudeGatewayHookResult(
                decision="block",
                reason="ONEX gateway guard could not read its settings. Check onex claude status and the hook mask file.",
            )
