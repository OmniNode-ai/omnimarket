# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Envelope-shaped handler for the ``tick`` operation of node_claude_token_pool_effect.

The runtime builds a routed handler from its injectable parameters alone. The rotation
handler takes ports (pool, login file, marks source, overlay), so this wrapper takes none
and assembles the production ports per request, through the same path the CLI tick uses.
"""

from __future__ import annotations

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationRequest,
    ModelClaudeTokenRotationResult,
)


class HandlerClaudeTokenRotationTick:
    """Runs one rotation tick for the request's host with the production ports."""

    def handle(
        self, request: ModelClaudeTokenRotationRequest
    ) -> ModelClaudeTokenRotationResult:
        from omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_token_pool import (
            run_tick,
        )

        return run_tick(request.host, dry_run=request.dry_run)
