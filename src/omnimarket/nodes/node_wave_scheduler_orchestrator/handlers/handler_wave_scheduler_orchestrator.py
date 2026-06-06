# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_wave_scheduler_orchestrator [OMN-12210].

ORCHESTRATOR node. Consumes ModelWaveSchedulerRequest, parses the plan file,
builds a dependency DAG, computes execution waves with configurable max
concurrency, and dispatches parallel ticket-pipeline workers per wave.

Bounded production slice: parse a plan, validate dependencies, compute waves,
and require an injected dispatcher adapter for live execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import yaml

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


class ProtocolWaveDispatcher(Protocol):
    """Adapter boundary for live ticket-pipeline wave dispatch."""

    def dispatch_wave(
        self, assignment: ModelWaveAssignment
    ) -> dict[str, EnumTicketExecutionStatus | str]: ...


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
                resumed=request.resume,
            )

        assignments = _build_wave_assignments(
            tickets,
            max_concurrency=request.max_concurrency,
            defer_repo_conflicts=request.defer_repo_conflicts,
        )
        if request.dry_run:
            return ModelWaveSchedulerResult(
                plan_path=request.plan_path,
                run_status=EnumWaveSchedulerStatus.DRY_RUN,
                wave_assignments=tuple(assignments),
                wave_execution_summaries=(),
                dependency_violations=(),
                total_tickets=len(tickets),
                dry_run=True,
                resumed=request.resume,
            )

        if self._dispatcher is None:
            raise RuntimeError("dispatcher adapter required when dry_run is false")

        summaries = [
            _execute_assignment(self._dispatcher, assignment)
            for assignment in assignments
        ]
        failed = sum(summary.failed_count for summary in summaries)
        blocked = sum(summary.blocked_count for summary in summaries)
        return ModelWaveSchedulerResult(
            plan_path=request.plan_path,
            run_status=(
                EnumWaveSchedulerStatus.COMPLETED
                if failed == 0 and blocked == 0
                else EnumWaveSchedulerStatus.PARTIAL
            ),
            wave_assignments=tuple(assignments),
            wave_execution_summaries=tuple(summaries),
            dependency_violations=(),
            total_tickets=len(tickets),
            tickets_completed=sum(summary.completed_count for summary in summaries),
            tickets_failed=failed,
            tickets_blocked=blocked,
            dry_run=False,
            resumed=request.resume,
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


def _build_wave_assignments(
    tickets: dict[str, dict[str, Any]],
    *,
    max_concurrency: int,
    defer_repo_conflicts: bool,
) -> list[ModelWaveAssignment]:
    remaining = set(tickets)
    completed: set[str] = set()
    assignments: list[ModelWaveAssignment] = []
    wave_id = 0

    while remaining:
        ready = sorted(
            ticket_id
            for ticket_id in remaining
            if set(tickets[ticket_id]["depends_on"]).issubset(completed)
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
        completed.update(selected)
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


def _execute_assignment(
    dispatcher: ProtocolWaveDispatcher, assignment: ModelWaveAssignment
) -> ModelWaveExecutionSummary:
    raw_statuses = dispatcher.dispatch_wave(assignment)
    statuses = tuple(
        (
            ticket_id,
            EnumTicketExecutionStatus(raw_statuses.get(ticket_id, "completed")),
        )
        for ticket_id in assignment.ticket_ids
    )
    return ModelWaveExecutionSummary(
        wave_id=assignment.wave_id,
        dispatched_count=len(assignment.ticket_ids),
        completed_count=sum(
            1 for _, status in statuses if status is EnumTicketExecutionStatus.COMPLETED
        ),
        failed_count=sum(
            1 for _, status in statuses if status is EnumTicketExecutionStatus.FAILED
        ),
        blocked_count=sum(
            1 for _, status in statuses if status is EnumTicketExecutionStatus.BLOCKED
        ),
        stalled_count=sum(
            1 for _, status in statuses if status is EnumTicketExecutionStatus.STALLED
        ),
        skipped_count=sum(
            1 for _, status in statuses if status is EnumTicketExecutionStatus.SKIPPED
        ),
        ticket_statuses=statuses,
    )


__all__ = ["HandlerWaveSchedulerOrchestrator", "ProtocolWaveDispatcher"]
