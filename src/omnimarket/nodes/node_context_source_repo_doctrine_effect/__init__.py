# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repository doctrine context source node: invariants, shapes and exemplars (OMN-21090)."""

from omnimarket.nodes.node_context_source_repo_doctrine_effect.handlers.handler_context_source_repo_doctrine import (
    HandlerContextSourceRepoDoctrine,
)


class NodeContextSourceRepoDoctrineEffect(HandlerContextSourceRepoDoctrine):
    """ONEX entry point."""


__all__ = ["HandlerContextSourceRepoDoctrine", "NodeContextSourceRepoDoctrineEffect"]
