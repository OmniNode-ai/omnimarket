# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_self_healing_dispatch_orchestrator [OMN-12208].

ORCHESTRATOR node. Consumes ModelSelfHealingDispatchRequest, orchestrates
self-healing ticket dispatch: resolve tickets (or decompose epic) → group by
repo → dispatch workers via TeamCreate → monitor stalls via agent_healthcheck
→ auto-recover stalls (bounded retry) → escalate exhausted tickets to Blocked.

Bounded production slice: validates input, groups tickets deterministically, and
requires an injected dispatcher adapter for live worker launch.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_request import (
    ModelSelfHealingDispatchRequest,
)
from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_result import (
    EnumDispatchRunStatus,
    EnumWorkerStatus,
    ModelDispatchGroup,
    ModelEscalationRecord,
    ModelSelfHealingDispatchResult,
    ModelWorkerRecord,
)


class ProtocolSelfHealingTicketResolver(Protocol):
    """Adapter boundary for resolving epic children into ticket IDs."""

    def resolve_epic_ticket_ids(self, epic_id: str) -> tuple[str, ...]: ...


class ProtocolSelfHealingDispatcher(Protocol):
    """Adapter boundary for live TeamCreate/worker dispatch side effects."""

    def dispatch_group(self, group: ModelDispatchGroup, *, run_id: str) -> str: ...


class HandlerSelfHealingDispatchOrchestrator:
    """ORCHESTRATOR — self-healing dispatch planner and adapter-gated launcher."""

    def __init__(
        self,
        ticket_resolver: ProtocolSelfHealingTicketResolver | None = None,
        dispatcher: ProtocolSelfHealingDispatcher | None = None,
    ) -> None:
        self._ticket_resolver = ticket_resolver
        self._dispatcher = dispatcher

    def handle(
        self, request: ModelSelfHealingDispatchRequest
    ) -> ModelSelfHealingDispatchResult:
        ticket_ids = _resolve_ticket_ids(request, self._ticket_resolver)
        run_id = request.run_id or _stable_run_id(ticket_ids, request.epic_id)
        groups = _group_tickets(ticket_ids, request.repo_hints, dry_run=request.dry_run)

        if request.dry_run:
            return ModelSelfHealingDispatchResult(
                run_id=run_id,
                run_status=EnumDispatchRunStatus.DRY_RUN,
                dispatch_groups=groups,
                dispatched_workers=(),
                stall_events=(),
                escalated_tickets=(),
                total_tickets=len(ticket_ids),
                stalls_recovered=0,
                elapsed_seconds=0,
                dry_run=True,
                log_path="",
            )

        if self._dispatcher is None:
            raise RuntimeError("dispatcher adapter required when dry_run is false")

        workers: list[ModelWorkerRecord] = []
        for group in groups:
            worker_name = self._dispatcher.dispatch_group(group, run_id=run_id)
            workers.append(
                ModelWorkerRecord(
                    worker_name=worker_name,
                    repo=group.repo,
                    ticket_ids=group.ticket_ids,
                    status=EnumWorkerStatus.COMPLETED,
                    redispatch_attempt=0,
                )
            )

        return ModelSelfHealingDispatchResult(
            run_id=run_id,
            run_status=EnumDispatchRunStatus.COMPLETED,
            dispatch_groups=groups,
            dispatched_workers=tuple(workers),
            stall_events=(),
            escalated_tickets=tuple(
                _escalation_record(worker)
                for worker in workers
                if worker.status is EnumWorkerStatus.ESCALATED
            ),
            total_tickets=len(ticket_ids),
            stalls_recovered=0,
            elapsed_seconds=0,
            dry_run=False,
            log_path="",
        )


def _resolve_ticket_ids(
    request: ModelSelfHealingDispatchRequest,
    ticket_resolver: ProtocolSelfHealingTicketResolver | None,
) -> tuple[str, ...]:
    has_tickets = bool(request.ticket_ids)
    has_epic = bool(request.epic_id)
    if has_tickets == has_epic:
        raise ValueError("Exactly one of ticket_ids or epic_id must be provided")
    if has_tickets:
        return tuple(dict.fromkeys(request.ticket_ids))
    if ticket_resolver is None:
        raise RuntimeError("ticket_resolver adapter required when epic_id is provided")
    return tuple(
        dict.fromkeys(ticket_resolver.resolve_epic_ticket_ids(request.epic_id))
    )


def _stable_run_id(ticket_ids: tuple[str, ...], epic_id: str) -> str:
    seed = epic_id or ",".join(ticket_ids)
    return f"self-healing-{uuid5(NAMESPACE_URL, seed)}"


def _group_tickets(
    ticket_ids: tuple[str, ...], repo_hints: dict[str, str], *, dry_run: bool
) -> tuple[ModelDispatchGroup, ...]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for ticket_id in ticket_ids:
        grouped[repo_hints.get(ticket_id, "unassigned")].append(ticket_id)
    return tuple(
        ModelDispatchGroup(
            repo=repo,
            ticket_ids=tuple(ids),
            worker_name=(f"dry-run-{repo}" if dry_run else ""),
        )
        for repo, ids in sorted(grouped.items())
    )


def _escalation_record(worker: ModelWorkerRecord) -> ModelEscalationRecord:
    return ModelEscalationRecord(
        ticket_id=worker.ticket_ids[0],
        repo=worker.repo,
        attempt_count=worker.redispatch_attempt,
    )


__all__ = [
    "HandlerSelfHealingDispatchOrchestrator",
    "ProtocolSelfHealingDispatcher",
    "ProtocolSelfHealingTicketResolver",
]
