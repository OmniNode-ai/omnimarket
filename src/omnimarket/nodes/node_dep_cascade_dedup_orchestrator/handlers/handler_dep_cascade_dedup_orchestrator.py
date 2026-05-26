# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler stub for node_dep_cascade_dedup_orchestrator [OMN-12213].

ORCHESTRATOR node. Consumes ModelDepCascadeDedupRequest, discovers open
automated dep-bump PRs across repos, groups by (repo, package), identifies
superseded PRs (all but the highest-version keeper, or all when the package
is already on main), and closes superseded PRs with a comment.

Implementation is deferred (node_not_implemented: true). This stub raises
NotImplementedError so the runtime fails loudly rather than silently misbehaving.
"""

from __future__ import annotations

from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_request import (
    ModelDepCascadeDedupRequest,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_result import (
    ModelDepCascadeDedupResult,
)


class HandlerDepCascadeDedupOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelDepCascadeDedupRequest
    ) -> ModelDepCascadeDedupResult:
        raise NotImplementedError(  # stub-ok
            "node_dep_cascade_dedup_orchestrator is not yet implemented (OMN-12213). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
