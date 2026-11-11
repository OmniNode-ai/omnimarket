# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude token pool revoke staleness compute node (OMN-23887)."""

from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.handlers.handler_revoke_staleness import (
    HandlerRevokeStaleness,
)


class NodeClaudePoolRevokeStalenessCompute(HandlerRevokeStaleness):
    """ONEX entrypoint for the pure revoke staleness decision."""


__all__ = ["HandlerRevokeStaleness", "NodeClaudePoolRevokeStalenessCompute"]
