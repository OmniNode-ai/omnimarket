# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_decompose_epic_orchestrator [OMN-12214].

ORCHESTRATOR node. The default implementation is intentionally bounded:
dry-run can return an injected deterministic plan, while live Linear/OCC
mutation requires injected adapters.

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

from typing import Protocol

from omnimarket.nodes.node_decompose_epic_orchestrator.models.model_decompose_epic_request import (
    ModelCreatedSubTicket,
    ModelDecomposeEpicRequest,
    ModelDecomposeEpicResult,
)


class ProtocolEpicDecompositionPlanner(Protocol):
    """Adapter boundary for deterministic epic decomposition planning."""

    def plan_subtickets(
        self, epic_id: str, max_tickets: int
    ) -> list[dict[str, str]]: ...


class ProtocolEpicTicketCreator(Protocol):
    """Adapter boundary for live Linear child ticket creation."""

    def create_subticket(
        self, epic_id: str, title: str, repo_hint: str
    ) -> dict[str, str]: ...


class HandlerDecomposeEpicOrchestrator:
    """ORCHESTRATOR — decomposes a Linear epic into atomic sub-tickets.

    No LLM, Linear, or OCC side effects happen unless adapters are injected.
    """

    def __init__(
        self,
        planner: ProtocolEpicDecompositionPlanner | None = None,
        ticket_creator: ProtocolEpicTicketCreator | None = None,
    ) -> None:
        self._planner = planner
        self._ticket_creator = ticket_creator

    async def handle(
        self, request: ModelDecomposeEpicRequest
    ) -> ModelDecomposeEpicResult:
        """Decompose a Linear epic into sub-tickets."""
        planned = (
            self._planner.plan_subtickets(request.epic_id, request.max_tickets)
            if self._planner is not None
            else []
        )[: request.max_tickets]

        if request.dry_run:
            return ModelDecomposeEpicResult(
                epic_id=request.epic_id,
                status="dry_run",
                created_tickets=tuple(
                    ModelCreatedSubTicket(
                        ticket_id=f"DRY-RUN-{index + 1}",
                        title=item["title"],
                        repo_hint=item.get("repo_hint", ""),
                        linear_id="",
                    )
                    for index, item in enumerate(planned)
                ),
                correlation_id=request.correlation_id,
            )

        if self._ticket_creator is None:
            raise RuntimeError("ticket_creator adapter required when dry_run is false")

        created = [
            self._ticket_creator.create_subticket(
                request.epic_id,
                item["title"],
                item.get("repo_hint", ""),
            )
            for item in planned
        ]
        return ModelDecomposeEpicResult(
            epic_id=request.epic_id,
            status="success",
            created_tickets=tuple(
                ModelCreatedSubTicket(
                    ticket_id=item["ticket_id"],
                    title=item["title"],
                    repo_hint=item.get("repo_hint", ""),
                    linear_id=item["linear_id"],
                )
                for item in created
            ),
            correlation_id=request.correlation_id,
        )


__all__ = [
    "HandlerDecomposeEpicOrchestrator",
    "ModelDecomposeEpicResult",
    "ProtocolEpicDecompositionPlanner",
    "ProtocolEpicTicketCreator",
]
