# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_generated_node_publish_effect package.

SEA canonicalization Phase 0.2 (OMN-13606): canonical EFFECT node that publishes
a generated node/package -- the auto-PR / publish step of the SEA self-extension
loop. Consumes the full canonical package staged by Phase 0.1
(``handler_generated_executor.scaffold_package``), creates a git worktree branch,
commits the package, pushes, opens a PR carrying the OMN ticket + dod_evidence,
and emits the PR URL on the contract-declared bus topic.
"""

from omnimarket.nodes.node_generated_node_publish_effect.handlers.handler_generated_node_publish_effect import (
    HandlerGeneratedNodePublishEffect,
)

__all__ = ["HandlerGeneratedNodePublishEffect"]
