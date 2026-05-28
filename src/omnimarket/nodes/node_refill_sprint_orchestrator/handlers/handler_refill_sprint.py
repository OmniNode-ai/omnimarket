# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRefillSprintOrchestrator — Sprint refill multi-phase orchestrator.

ONEX node type: ORCHESTRATOR — impure, effectful, multi-phase.

Bounded production slice: scores injected backlog candidates deterministically.
Live Linear mutation requires an injected adapter.

Algorithm phases (per refill_sprint SKILL.md):
  1. Capacity check   — sum weighted estimates for Active Sprint tickets in Backlog/Todo.
                        Exit early if capacity >= threshold.
  2. Candidate selection — query Future project; tier-1/2/3 + hard gates.
  3. Scope verification  — verify file/API refs in ticket description still exist.
  4. Pull and label      — move to Active Sprint, add `auto-pulled`, set priority.
  5. Notification        — emit sprint.auto-pull.completed Kafka event.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_refill_sprint_orchestrator.models.model_refill_sprint import (
    ModelBacklogFilter,
    ModelPriorityWeights,
    ModelPulledTicket,
    ModelRefillSprintResult,
    ModelSkippedTicket,
    ModelSprintCapacityConfig,
)

# ---------------------------------------------------------------------------
# Request model (lives here so contract.yaml input_model path is canonical)
# ---------------------------------------------------------------------------


class ModelRefillSprintRequest(BaseModel):
    """Input envelope for the sprint refill orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capacity_config: ModelSprintCapacityConfig = Field(
        default_factory=ModelSprintCapacityConfig,
        description="Sprint capacity threshold, batch size, and dry-run flag.",
    )
    backlog_filter: ModelBacklogFilter = Field(
        default_factory=ModelBacklogFilter,
        description="Candidate selection filter: team, project, excluded labels.",
    )
    priority_weights: ModelPriorityWeights = Field(
        default_factory=ModelPriorityWeights,
        description="Tier scoring weights used to rank candidates.",
    )


class ProtocolSprintBacklogAdapter(Protocol):
    """Adapter boundary for sprint capacity, candidates, and live ticket moves."""

    def current_capacity(self) -> float: ...

    def candidate_tickets(
        self, backlog_filter: ModelBacklogFilter
    ) -> list[dict[str, Any]]: ...

    def pull_ticket(self, ticket_id: str) -> None: ...


class HandlerRefillSprintOrchestrator:
    """ORCHESTRATOR — multi-phase sprint refill pipeline.

    Dry-run returns the pull plan. Live ticket movement only occurs through an
    injected ``ProtocolSprintBacklogAdapter``.
    """

    def __init__(self, adapter: ProtocolSprintBacklogAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(self, request: ModelRefillSprintRequest) -> ModelRefillSprintResult:
        """Execute the sprint refill pipeline.

        Raises:
            RuntimeError: when capacity/candidate data or live mutation needs an adapter.
        """
        if self._adapter is None:
            if request.capacity_config.dry_run:
                return ModelRefillSprintResult(
                    pulled=[],
                    skipped=[],
                    pulled_count=0,
                    skipped_count=0,
                    exhausted=True,
                    capacity_before=0.0,
                    capacity_after=0.0,
                    dry_run=True,
                )
            raise RuntimeError("backlog adapter required when dry_run is false")

        capacity_before = self._adapter.current_capacity()
        if capacity_before >= request.capacity_config.threshold:
            return ModelRefillSprintResult(
                pulled=[],
                skipped=[],
                pulled_count=0,
                skipped_count=0,
                exhausted=False,
                capacity_before=capacity_before,
                capacity_after=capacity_before,
                dry_run=request.capacity_config.dry_run,
            )

        candidates = self._adapter.candidate_tickets(request.backlog_filter)
        pulled, skipped = _select_candidates(
            candidates,
            request.priority_weights,
            batch_size=request.capacity_config.batch_size,
            skip_scope_check=request.capacity_config.skip_scope_check,
        )

        if not request.capacity_config.dry_run:
            for ticket in pulled:
                self._adapter.pull_ticket(ticket.ticket_id)

        capacity_after = capacity_before + float(len(pulled))
        return ModelRefillSprintResult(
            pulled=pulled,
            skipped=skipped,
            pulled_count=len(pulled),
            skipped_count=len(skipped),
            exhausted=not pulled,
            capacity_before=capacity_before,
            capacity_after=capacity_after,
            dry_run=request.capacity_config.dry_run,
        )


def _select_candidates(
    candidates: list[dict[str, Any]],
    weights: ModelPriorityWeights,
    *,
    batch_size: int,
    skip_scope_check: bool,
) -> tuple[list[ModelPulledTicket], list[ModelSkippedTicket]]:
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    skipped: list[ModelSkippedTicket] = []
    for candidate in candidates:
        ticket_id = str(candidate["ticket_id"])
        title = str(candidate.get("title", ""))
        skip_reason = _skip_reason(candidate, skip_scope_check)
        if skip_reason:
            skipped.append(
                ModelSkippedTicket(
                    ticket_id=ticket_id,
                    title=title,
                    reason=skip_reason,
                )
            )
            continue
        tier = int(candidate.get("tier", 3))
        score = _score(tier, float(candidate.get("age_weeks", 0.0)), weights)
        ranked.append((score, ticket_id, candidate))

    pulled: list[ModelPulledTicket] = []
    for score, _ticket_id, candidate in sorted(
        ranked, key=lambda item: (-item[0], item[1])
    ):
        if len(pulled) >= batch_size:
            skipped.append(
                ModelSkippedTicket(
                    ticket_id=str(candidate["ticket_id"]),
                    title=str(candidate.get("title", "")),
                    reason="batch_limit_reached",
                )
            )
            continue
        pulled.append(
            ModelPulledTicket(
                ticket_id=str(candidate["ticket_id"]),
                title=str(candidate.get("title", "")),
                tier=int(candidate.get("tier", 3)),
                priority_score=score,
                estimate_label=(
                    str(candidate["estimate_label"])
                    if candidate.get("estimate_label") is not None
                    else None
                ),
                scope_verified=skip_scope_check
                or bool(candidate.get("scope_verified", True)),
            )
        )
    return pulled, skipped


def _skip_reason(candidate: dict[str, Any], skip_scope_check: bool) -> str | None:
    if candidate.get("estimate_too_large"):
        return "estimate_too_large"
    if candidate.get("urgent"):
        return "urgent_priority"
    if candidate.get("cross_repo_dependency"):
        return "cross_repo_dependency"
    if candidate.get("failed_attempts", 0) >= 2:
        return "too_many_failed_attempts"
    if candidate.get("returned_today"):
        return "returned_today"
    if not skip_scope_check and candidate.get("scope_verified") is False:
        return "stale_scope"
    return None


def _score(tier: int, age_weeks: float, weights: ModelPriorityWeights) -> float:
    tier_weight = {
        1: weights.tier_1,
        2: weights.tier_2,
        3: weights.tier_3,
    }.get(tier, weights.tier_3)
    return tier_weight + (age_weeks * weights.recency_boost)


__all__ = [
    "HandlerRefillSprintOrchestrator",
    "ModelRefillSprintRequest",
    "ProtocolSprintBacklogAdapter",
]
