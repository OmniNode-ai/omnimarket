# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_wave_scheduler_orchestrator [OMN-12210, repaired OMN-17017].

ORCHESTRATOR node. Consumes ModelWaveSchedulerRequest, parses the plan file,
builds a dependency DAG, and dispatches one wave at a time — each wave computed
against **observed** completion, never against selection.

OMN-17017 repaired four defects the contract concealed (2026-08-29 beta
off-the-rails analysis rev 2, §RC-J):

* the schedule was fully computed before the first ticket ran, with
  ``completed.update(selected)`` making *selection* equal *completion*;
* failed dependencies did not suppress downstream dispatch — the execution loop
  was an unconditional comprehension over the pre-computed assignment list;
* a missing dispatcher status silently became ``"completed"``;
* ``fail_fast`` was a live CLI flag with zero handler references, and ``resumed``
  was reported ``true`` for a run that resumed nothing.

Waves are now derived incrementally: dispatch the ready set, read back what the
dispatcher observed, promote only observed COMPLETED tickets, and remove the
transitive dependent closure of everything else from the remaining waves.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnimarket.nodes.node_wave_scheduler_orchestrator.handlers.handler_wave_dispatch_state_store import (
    HandlerWaveDispatchStateStore,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_request import (
    ModelWaveSchedulerRequest,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_result import (
    EnumDependencyViolationKind,
    EnumTicketExecutionStatus,
    EnumWaveSchedulerStatus,
    ModelDependencyViolation,
    ModelWaveAssignment,
    ModelWaveExecutionSummary,
    ModelWaveSchedulerResult,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.wave_state import (
    ModelWaveCheckpoint,
    load_checkpoint,
    resolve_state_dir,
    run_id_for,
    write_checkpoint,
)

# Only an observed COMPLETED unblocks a dependent. Everything else — FAILED,
# BLOCKED, STALLED, TIMEOUT, SKIPPED, DEFERRED, UNREPORTED — cascades.
_UNBLOCKING = EnumTicketExecutionStatus.COMPLETED


class ProtocolWaveDispatcher(Protocol):
    """Adapter boundary for live ticket-pipeline wave dispatch.

    A dispatcher returns statuses **only** for tickets whose terminal outcome it
    actually observed. Omitting a ticket is the honest answer for "dispatched,
    not acknowledged"; the orchestrator records it UNREPORTED and blocks its
    dependents. It must never invent a status.
    """

    def dispatch_wave(
        self, assignment: ModelWaveAssignment
    ) -> Mapping[str, EnumTicketExecutionStatus]: ...


class HandlerWaveSchedulerOrchestrator:
    """ORCHESTRATOR — dependency-aware wave scheduler."""

    def __init__(self, dispatcher: ProtocolWaveDispatcher | None = None) -> None:
        self._dispatcher = dispatcher

    def handle(self, request: ModelWaveSchedulerRequest) -> ModelWaveSchedulerResult:
        tickets = _read_plan(Path(request.plan_path))
        violations = _dependency_violations(tickets)
        if violations:
            return ModelWaveSchedulerResult(
                plan_path=request.plan_path,
                run_status=EnumWaveSchedulerStatus.FAILED,
                wave_assignments=(),
                wave_execution_summaries=(),
                dependency_violations=tuple(violations),
                total_tickets=len(tickets),
                dry_run=request.dry_run,
                resumed=False,
            )

        if request.dry_run:
            return ModelWaveSchedulerResult(
                plan_path=request.plan_path,
                run_status=EnumWaveSchedulerStatus.DRY_RUN,
                wave_assignments=tuple(
                    _projected_wave_assignments(
                        tickets,
                        max_concurrency=request.max_concurrency,
                        defer_repo_conflicts=request.defer_repo_conflicts,
                    )
                ),
                wave_execution_summaries=(),
                dependency_violations=(),
                total_tickets=len(tickets),
                dry_run=True,
                resumed=False,
            )

        return self._execute(request, tickets)

    # -- live execution ----------------------------------------------------

    def _execute(
        self,
        request: ModelWaveSchedulerRequest,
        tickets: dict[str, dict[str, Any]],
    ) -> ModelWaveSchedulerResult:
        state_dir = resolve_state_dir(request.state_dir)
        run_id = run_id_for(request.plan_path)
        checkpoint = load_checkpoint(state_dir, run_id) if request.resume else None
        resumed = checkpoint is not None

        dispatcher = self._dispatcher or HandlerWaveDispatchStateStore(
            state_dir=state_dir, run_id=run_id
        )

        completed: set[str] = {
            ticket_id
            for ticket_id in (checkpoint.completed_ticket_ids if checkpoint else ())
            if ticket_id in tickets
        }
        remaining = set(tickets) - completed
        assignments: list[ModelWaveAssignment] = []
        summaries: list[ModelWaveExecutionSummary] = []
        terminal: dict[str, EnumTicketExecutionStatus] = {}
        aborted = False
        wave_id = 0

        while remaining:
            ready = sorted(
                ticket_id
                for ticket_id in remaining
                if set(tickets[ticket_id]["depends_on"]).issubset(completed)
            )
            if not ready:
                # Everything left depends on something that did not complete.
                break
            selected, deferred = _select_wave_tickets(
                ready,
                tickets,
                max_concurrency=request.max_concurrency,
                defer_repo_conflicts=request.defer_repo_conflicts,
            )
            assignment = ModelWaveAssignment(
                wave_id=wave_id,
                ticket_ids=tuple(selected),
                repo_assignments=tuple(
                    (ticket_id, str(tickets[ticket_id]["repo"]))
                    for ticket_id in selected
                    if tickets[ticket_id]["repo"]
                ),
                deferred_ticket_ids=tuple(deferred),
            )
            assignments.append(assignment)

            statuses = _observed_statuses(dispatcher, assignment)
            summaries.append(_summarise(assignment, statuses))
            terminal.update(statuses)
            remaining.difference_update(selected)

            wave_completed = {
                ticket_id
                for ticket_id, status in statuses.items()
                if status is _UNBLOCKING
            }
            completed.update(wave_completed)
            write_checkpoint(
                state_dir,
                ModelWaveCheckpoint(
                    run_id=run_id,
                    plan_path=request.plan_path,
                    completed_ticket_ids=tuple(sorted(completed)),
                ),
            )

            if request.fail_fast and len(wave_completed) != len(selected):
                aborted = True
                break
            wave_id += 1

        blocked = _dependent_closure(tickets, remaining, completed)
        for ticket_id in sorted(blocked):
            terminal[ticket_id] = EnumTicketExecutionStatus.BLOCKED
        skipped = sorted(remaining - blocked)
        for ticket_id in skipped:
            terminal[ticket_id] = EnumTicketExecutionStatus.SKIPPED
        if blocked or skipped:
            # Keyed off the number of dispatched waves, not the loop counter: a
            # fail_fast break leaves wave_id un-incremented and would collide.
            summaries.append(
                _undispatched_summary(len(assignments), sorted(blocked), skipped)
            )

        return _result(
            request=request,
            total_tickets=len(tickets),
            assignments=assignments,
            summaries=summaries,
            terminal=terminal,
            aborted=aborted,
            resumed=resumed,
            dispatcher=dispatcher,
        )


def _observed_statuses(
    dispatcher: ProtocolWaveDispatcher, assignment: ModelWaveAssignment
) -> dict[str, EnumTicketExecutionStatus]:
    """Coerce the dispatcher's report, defaulting nothing and inventing nothing."""
    reported = dispatcher.dispatch_wave(assignment)
    statuses: dict[str, EnumTicketExecutionStatus] = {}
    for ticket_id in assignment.ticket_ids:
        if ticket_id not in reported:
            statuses[ticket_id] = EnumTicketExecutionStatus.UNREPORTED
            continue
        statuses[ticket_id] = EnumTicketExecutionStatus(reported[ticket_id])
    return statuses


def _summarise(
    assignment: ModelWaveAssignment,
    statuses: Mapping[str, EnumTicketExecutionStatus],
) -> ModelWaveExecutionSummary:
    ordered = tuple(
        (ticket_id, statuses[ticket_id]) for ticket_id in assignment.ticket_ids
    )

    def _count(target: EnumTicketExecutionStatus) -> int:
        return sum(1 for _, status in ordered if status is target)

    return ModelWaveExecutionSummary(
        wave_id=assignment.wave_id,
        dispatched_count=len(ordered),
        completed_count=_count(EnumTicketExecutionStatus.COMPLETED),
        failed_count=_count(EnumTicketExecutionStatus.FAILED),
        blocked_count=_count(EnumTicketExecutionStatus.BLOCKED),
        stalled_count=_count(EnumTicketExecutionStatus.STALLED),
        skipped_count=_count(EnumTicketExecutionStatus.SKIPPED),
        unreported_count=_count(EnumTicketExecutionStatus.UNREPORTED),
        ticket_statuses=ordered,
    )


def _undispatched_summary(
    wave_id: int, blocked: list[str], skipped: list[str]
) -> ModelWaveExecutionSummary:
    """A zero-dispatch summary carrying the cascade — visible, not silent."""
    ordered = tuple(
        [(ticket_id, EnumTicketExecutionStatus.BLOCKED) for ticket_id in blocked]
        + [(ticket_id, EnumTicketExecutionStatus.SKIPPED) for ticket_id in skipped]
    )
    return ModelWaveExecutionSummary(
        wave_id=wave_id,
        dispatched_count=0,
        blocked_count=len(blocked),
        skipped_count=len(skipped),
        ticket_statuses=ordered,
    )


def _dependent_closure(
    tickets: dict[str, dict[str, Any]],
    remaining: set[str],
    completed: set[str],
) -> set[str]:
    """Undispatched tickets that transitively depend on a non-completion."""
    blocked: set[str] = set()
    changed = True
    while changed:
        changed = False
        for ticket_id in sorted(remaining - blocked):
            dependencies = set(tickets[ticket_id]["depends_on"])
            unmet = dependencies - completed
            if unmet:
                blocked.add(ticket_id)
                changed = True
    return blocked


def _result(
    *,
    request: ModelWaveSchedulerRequest,
    total_tickets: int,
    assignments: list[ModelWaveAssignment],
    summaries: list[ModelWaveExecutionSummary],
    terminal: Mapping[str, EnumTicketExecutionStatus],
    aborted: bool,
    resumed: bool,
    dispatcher: ProtocolWaveDispatcher,
) -> ModelWaveSchedulerResult:
    def _count(target: EnumTicketExecutionStatus) -> int:
        return sum(1 for status in terminal.values() if status is target)

    failed = _count(EnumTicketExecutionStatus.FAILED)
    blocked = _count(EnumTicketExecutionStatus.BLOCKED)
    unreported = _count(EnumTicketExecutionStatus.UNREPORTED)
    skipped = _count(EnumTicketExecutionStatus.SKIPPED)
    clean = all(status is _UNBLOCKING for status in terminal.values())

    if aborted:
        run_status = EnumWaveSchedulerStatus.ABORTED
    elif clean:
        run_status = EnumWaveSchedulerStatus.COMPLETED
    else:
        run_status = EnumWaveSchedulerStatus.PARTIAL

    lifecycle_path = (
        str(dispatcher.lifecycle_path)
        if isinstance(dispatcher, HandlerWaveDispatchStateStore)
        else None
    )
    return ModelWaveSchedulerResult(
        plan_path=request.plan_path,
        run_status=run_status,
        wave_assignments=tuple(assignments),
        wave_execution_summaries=tuple(summaries),
        dependency_violations=(),
        total_tickets=total_tickets,
        tickets_completed=_count(_UNBLOCKING),
        tickets_failed=failed,
        tickets_blocked=blocked,
        tickets_unreported=unreported,
        tickets_skipped=skipped,
        dispatch_lifecycle_path=lifecycle_path,
        dry_run=False,
        resumed=resumed,
    )


def _read_plan(path: Path) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("plan file must contain a mapping")
    rows = raw.get("tickets", raw.get("tasks", []))
    if not isinstance(rows, list):
        raise ValueError("plan tickets/tasks must be a list")

    tickets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each plan ticket must be a mapping")
        ticket_id = str(row.get("ticket_id") or row.get("id") or "").strip()
        if not ticket_id:
            raise ValueError("each plan ticket must include ticket_id or id")
        depends_on = row.get("depends_on", [])
        if depends_on is None:
            depends_on = []
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        if not isinstance(depends_on, list):
            raise ValueError(f"{ticket_id} depends_on must be a list")
        tickets[ticket_id] = {
            "repo": str(row.get("repo") or row.get("repository") or ""),
            "depends_on": tuple(str(item) for item in depends_on),
        }
    return tickets


def _dependency_violations(
    tickets: dict[str, dict[str, Any]],
) -> list[ModelDependencyViolation]:
    violations: list[ModelDependencyViolation] = []
    for ticket_id, data in sorted(tickets.items()):
        for dependency_id in data["depends_on"]:
            if dependency_id == ticket_id:
                violations.append(
                    ModelDependencyViolation(
                        kind=EnumDependencyViolationKind.SELF_REFERENCE,
                        ticket_id=ticket_id,
                        dependency_id=dependency_id,
                        message=f"{ticket_id} depends on itself",
                    )
                )
            elif dependency_id not in tickets:
                violations.append(
                    ModelDependencyViolation(
                        kind=EnumDependencyViolationKind.MISSING_DEPENDENCY,
                        ticket_id=ticket_id,
                        dependency_id=dependency_id,
                        message=f"{ticket_id} depends on missing {dependency_id}",
                    )
                )
    cycle = _find_cycle(tickets)
    if cycle:
        violations.append(
            ModelDependencyViolation(
                kind=EnumDependencyViolationKind.CYCLE,
                ticket_id=cycle[0],
                cycle_path=tuple(cycle),
                message=f"dependency cycle detected: {' -> '.join(cycle)}",
            )
        )
    return violations


def _find_cycle(tickets: dict[str, dict[str, Any]]) -> list[str]:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(ticket_id: str) -> list[str]:
        if ticket_id in visited:
            return []
        if ticket_id in visiting:
            start = visiting.index(ticket_id)
            return [*visiting[start:], ticket_id]
        visiting.append(ticket_id)
        for dependency_id in tickets[ticket_id]["depends_on"]:
            if dependency_id in tickets:
                cycle = visit(dependency_id)
                if cycle:
                    return cycle
        visiting.pop()
        visited.add(ticket_id)
        return []

    for ticket_id in sorted(tickets):
        cycle = visit(ticket_id)
        if cycle:
            return cycle
    return []


def _projected_wave_assignments(
    tickets: dict[str, dict[str, Any]],
    *,
    max_concurrency: int,
    defer_repo_conflicts: bool,
) -> list[ModelWaveAssignment]:
    """Dry-run PROJECTION of the wave schedule.

    This is the one place where selection may stand in for completion, because
    nothing is dispatched: it answers "what would run, assuming every wave
    succeeds". Live execution never uses it (OMN-17017).
    """
    remaining = set(tickets)
    assumed_completed: set[str] = set()
    assignments: list[ModelWaveAssignment] = []
    wave_id = 0

    while remaining:
        ready = sorted(
            ticket_id
            for ticket_id in remaining
            if set(tickets[ticket_id]["depends_on"]).issubset(assumed_completed)
        )
        if not ready:
            raise ValueError("dependency graph has no ready tickets")
        selected, deferred = _select_wave_tickets(
            ready,
            tickets,
            max_concurrency=max_concurrency,
            defer_repo_conflicts=defer_repo_conflicts,
        )
        assignments.append(
            ModelWaveAssignment(
                wave_id=wave_id,
                ticket_ids=tuple(selected),
                repo_assignments=tuple(
                    (ticket_id, str(tickets[ticket_id]["repo"]))
                    for ticket_id in selected
                    if tickets[ticket_id]["repo"]
                ),
                deferred_ticket_ids=tuple(deferred),
            )
        )
        remaining.difference_update(selected)
        assumed_completed.update(selected)
        wave_id += 1
    return assignments


def _select_wave_tickets(
    ready: list[str],
    tickets: dict[str, dict[str, Any]],
    *,
    max_concurrency: int,
    defer_repo_conflicts: bool,
) -> tuple[list[str], list[str]]:
    selected: list[str] = []
    deferred: list[str] = []
    used_repos: set[str] = set()
    for ticket_id in ready:
        repo = str(tickets[ticket_id]["repo"])
        if len(selected) >= max_concurrency:
            deferred.append(ticket_id)
            continue
        if defer_repo_conflicts and repo and repo in used_repos:
            deferred.append(ticket_id)
            continue
        selected.append(ticket_id)
        if repo:
            used_repos.add(repo)
    return selected, deferred


__all__ = ["HandlerWaveSchedulerOrchestrator", "ProtocolWaveDispatcher"]
