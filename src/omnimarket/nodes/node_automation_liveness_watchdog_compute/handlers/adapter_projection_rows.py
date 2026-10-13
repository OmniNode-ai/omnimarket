# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map the liveness projection's fold rows onto the watchdog's process facts.

The watchdog evaluates over the same fold the projection writer keeps (4.5:
one fold, not a second implementation). The fold's state row and run history
are described here by protocols naming exactly the fields read, so this
mapping is typed and tested against stand-in rows before the projection node
exists in this tree, and binds to the projection's own row models unchanged
once it does.

Only the verdicts an observer alone can produce are carried over as observer
reports. The state row's verdict is also where the watchdog's own verdict
events fold, and carrying those back in would make every UNOBSERVABLE the
watchdog computed from the runs sticky.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelObserverReport,
    ModelProcessRunFacts,
    ModelRunFact,
    ModelUndeclaredProcess,
)

#: Runs kept per process: the failure window's six and the idle streak.
KEPT_RUNS = 12

#: Reasons only an observer publishes; the watchdog never derives them.
OBSERVER_ONLY_REASONS: frozenset[EnumAutomationLivenessReason] = frozenset(
    {
        EnumAutomationLivenessReason.WORKFLOW_DISABLED,
        EnumAutomationLivenessReason.EVIDENCE_UNREADABLE,
        EnumAutomationLivenessReason.SOURCE_STALE,
        EnumAutomationLivenessReason.READ_INCOMPLETE,
    }
)


class ProtocolLivenessStateRow(Protocol):
    """The fields of the projection's per-process state row the watchdog reads."""

    @property
    def process_id(self) -> str: ...
    @property
    def host(self) -> str: ...
    @property
    def declared_at(self) -> datetime | None: ...
    @property
    def process_state(self) -> EnumAutomationProcessState | None: ...
    @property
    def last_run_at(self) -> datetime | None: ...
    @property
    def last_demand_count(self) -> int | None: ...
    @property
    def last_heartbeat_at(self) -> datetime | None: ...
    @property
    def last_progress_at(self) -> datetime | None: ...
    @property
    def progress_counter(self) -> int | None: ...
    @property
    def open_run_started_at(self) -> datetime | None: ...
    @property
    def verdict(self) -> EnumAutomationLivenessVerdict | None: ...
    @property
    def verdict_reason(self) -> EnumAutomationLivenessReason | None: ...
    @property
    def verdict_evaluated_at(self) -> datetime | None: ...


class ProtocolLivenessRunRow(Protocol):
    """The fields of the projection's run-history row the watchdog reads."""

    @property
    def process_id(self) -> str: ...
    @property
    def host(self) -> str: ...
    @property
    def run_id(self) -> str: ...
    @property
    def phase(self) -> EnumAutomationRunPhase: ...
    @property
    def started_at(self) -> datetime: ...
    @property
    def finished_at(self) -> datetime | None: ...
    @property
    def outcome(self) -> EnumAutomationRunOutcome | None: ...
    @property
    def did_work_count(self) -> int | None: ...
    @property
    def demand_count(self) -> int | None: ...
    @property
    def unseen_runs(self) -> int: ...


def _observer_report(row: ProtocolLivenessStateRow) -> ModelObserverReport | None:
    reason = row.verdict_reason
    if row.verdict is None or reason is None or reason not in OBSERVER_ONLY_REASONS:
        return None
    reported_at = row.verdict_evaluated_at
    if reported_at is None:
        return None
    return ModelObserverReport(
        verdict=row.verdict, reason=reason, reported_at=reported_at
    )


def run_facts(
    runs: Iterable[ProtocolLivenessRunRow], *, keep: int = KEPT_RUNS
) -> tuple[ModelRunFact, ...]:
    """The newest ``keep`` finished runs, oldest first, one per run id."""
    finished: dict[str, ModelRunFact] = {}
    for row in runs:
        if row.phase is not EnumAutomationRunPhase.FINISHED:
            continue
        if row.finished_at is None or row.outcome is None:
            continue
        finished[row.run_id] = ModelRunFact(
            run_id=row.run_id,
            started_at=row.started_at,
            finished_at=row.finished_at,
            outcome=row.outcome,
            did_work_count=row.did_work_count or 0,
            demand_count=row.demand_count,
            unseen_runs=row.unseen_runs,
        )
    ordered = sorted(finished.values(), key=lambda r: (r.finished_at, r.run_id))
    return tuple(ordered[-keep:])


def facts_from_projection(
    row: ProtocolLivenessStateRow,
    runs: Iterable[ProtocolLivenessRunRow],
    *,
    keep: int = KEPT_RUNS,
) -> ModelProcessRunFacts:
    """One declared process's facts from its state row and its run history."""
    mine = [r for r in runs if (r.process_id, r.host) == (row.process_id, row.host)]
    return ModelProcessRunFacts(
        process_id=row.process_id,
        host=row.host,
        declared_at=row.declared_at,
        runs=run_facts(mine, keep=keep),
        open_run_started_at=row.open_run_started_at,
        last_run_at=row.last_run_at,
        last_heartbeat_at=row.last_heartbeat_at,
        last_progress_at=row.last_progress_at,
        progress_counter=row.progress_counter,
        heartbeat_demand_count=row.last_demand_count
        if row.last_heartbeat_at is not None
        else None,
        observer_report=_observer_report(row),
    )


def undeclared_from_projection(
    row: ProtocolLivenessStateRow,
) -> ModelUndeclaredProcess | None:
    """An observer's UNDECLARED report kept in a row the overlay never declared."""
    if row.process_state is not None:
        return None
    if row.verdict is not EnumAutomationLivenessVerdict.UNDECLARED:
        return None
    reported_at = row.verdict_evaluated_at
    if reported_at is None:
        return None
    return ModelUndeclaredProcess(
        process_id=row.process_id, host=row.host, reported_at=reported_at
    )


__all__ = [
    "KEPT_RUNS",
    "OBSERVER_ONLY_REASONS",
    "ProtocolLivenessRunRow",
    "ProtocolLivenessStateRow",
    "facts_from_projection",
    "run_facts",
    "undeclared_from_projection",
]
