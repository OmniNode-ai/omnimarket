# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_decompose_epic_orchestrator [OMN-12214].

ORCHESTRATOR node. Fetches a Linear epic by ID, analyzes its description and
goals via LLM to generate atomic sub-ticket specs, creates each sub-ticket as a
Linear child of the epic, and optionally generates OCC contract YAML stubs for
each created ticket.

Algorithm (from decompose_epic SKILL.md):
  1. Fetch epic from Linear (includeRelations=true)
  2. Read repo_manifest.yaml for keyword-to-repo mapping
  3. Analyze epic description + goals to identify distinct workstreams
  4. If dry_run: return plan without creating tickets
  5. Create each ticket via Linear API with parentId set to epic
  6. If generate_contracts: generate OCC contract stubs, commit, open PR
  7. Emit ModelDecomposeEpicResult as terminal event
"""

from __future__ import annotations

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_decompose_epic_orchestrator.models.model_decompose_epic_request import (
    ModelDecomposeEpicRequest,
    ModelDecomposeEpicResult,
)


class HandlerDecomposeEpicOrchestrator:
    """ORCHESTRATOR — decomposes a Linear epic into atomic sub-tickets.

    Stub implementation. Full implementation tracked in OMN-12214.
    """

    async def handle(  # type: ignore[return]
        self, request: ModelDecomposeEpicRequest
    ) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Decompose a Linear epic into sub-tickets."""
        raise NotImplementedError(  # stub-ok
            "HandlerDecomposeEpicOrchestrator is not yet implemented (OMN-12214). "
            "This stub exists to establish the contract, models, and handler surface."
        )


__all__ = ["HandlerDecomposeEpicOrchestrator", "ModelDecomposeEpicResult"]
