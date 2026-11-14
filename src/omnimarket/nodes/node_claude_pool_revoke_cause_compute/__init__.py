# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude token pool revoke cause compute node (OMN-19998)."""

from omnimarket.nodes.node_claude_pool_revoke_cause_compute.handlers.handler_revoke_cause import (
    HandlerRevokeCause,
)


class NodeClaudePoolRevokeCauseCompute(HandlerRevokeCause):
    """ONEX entrypoint for the pure revoke cause decision."""


__all__ = ["HandlerRevokeCause", "NodeClaudePoolRevokeCauseCompute"]
