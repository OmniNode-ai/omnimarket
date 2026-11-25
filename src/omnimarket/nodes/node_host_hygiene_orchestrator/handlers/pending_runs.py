# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled runs whose lab work units are out on the bus and not yet all answered.

The tick route publishes each due unit and returns; the hosts answer on the lab work terminal topics, and
the receipt handlers record each answer here. The run is complete when its last unit has answered, or when a
later tick finds its deadline past (a host that never answers must not keep a run open forever).

One store is shared by the tick route and the receipt handlers of a runtime process, the way
``HOST_DISK_READINGS`` is shared by the reading handlers. ``dispatched`` is the time each (host, cleanup)
last ran, which the decision reads for the cleanup's own interval.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from omnimarket.models.model_host_hygiene import ModelHostHygieneDecisionItem
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneUnitResult,
)

#: How long past its longest unit timeout a run waits for the last terminal before a tick settles it.
DEADLINE_SLACK_SECONDS = 600

#: The outcomes that mean the command ran on the host, so the cleanup waits its interval.
RAN = frozenset(
    {
        EnumHostHygieneUnitOutcome.COMPLETED,
        EnumHostHygieneUnitOutcome.FAILED,
        EnumHostHygieneUnitOutcome.TIMED_OUT,
    }
)


@dataclass
class PendingRun:
    """One scheduled run: its slots in decision order, each a result or an open work unit id."""

    run_id: str
    job_id: str
    tick_id: str
    started_at: dt.datetime
    scheduled_for: dt.datetime
    deadline: dt.datetime
    decided: tuple[ModelHostHygieneDecisionItem, ...]
    slots: list[ModelHostHygieneUnitResult | str]
    open_units: dict[str, tuple[str, str]] = field(default_factory=dict)

    def results(self) -> tuple[ModelHostHygieneUnitResult, ...]:
        return tuple(s for s in self.slots if isinstance(s, ModelHostHygieneUnitResult))

    def settle(self, result: ModelHostHygieneUnitResult) -> None:
        for index, slot in enumerate(self.slots):
            if slot == result.work_unit_id:
                self.slots[index] = result
        self.open_units.pop(result.work_unit_id, None)


class PendingHostHygieneRuns:
    """The open scheduled runs and the last-ran times of the cleanups."""

    def __init__(self) -> None:
        self.dispatched: dict[tuple[str, str], dt.datetime] = {}
        self._runs: dict[str, PendingRun] = {}
        self._by_unit: dict[str, str] = {}

    def clear(self) -> None:
        """Forget every open run and last-ran time (a test's fresh start)."""
        self.dispatched.clear()
        self._runs.clear()
        self._by_unit.clear()

    def open_runs(self) -> int:
        return len(self._runs)

    def add(self, run: PendingRun) -> None:
        self._runs[run.run_id] = run
        for unit_id in run.open_units:
            self._by_unit[unit_id] = run.run_id

    def open_unit(self, work_unit_id: str) -> tuple[str, str] | None:
        """The (host, cleanup_id) of a unit this store is waiting on, or None."""
        run_id = self._by_unit.get(work_unit_id)
        run = self._runs.get(run_id) if run_id is not None else None
        return run.open_units.get(work_unit_id) if run is not None else None

    def record(
        self, result: ModelHostHygieneUnitResult
    ) -> tuple[PendingRun, bool] | None:
        """Record one unit's answer; return its run and whether the run is now complete.

        An answer for a unit this store does not hold returns None (the terminal topics carry every lab
        work sender's units). A cleanup that ran is remembered with the run's start; one that never
        reached its host is not, so the next due tick sends it again.
        """
        run_id = self._by_unit.pop(result.work_unit_id, None)
        run = self._runs.get(run_id) if run_id is not None else None
        if run is None:
            return None
        run.settle(result)
        if result.outcome in RAN:
            self.dispatched[(result.host, result.cleanup_id)] = run.started_at
        complete = not run.open_units
        if complete:
            del self._runs[run.run_id]
        return run, complete

    def expire(self, now: dt.datetime, *, detail: str) -> tuple[PendingRun, ...]:
        """Close the runs whose deadline is past; each open unit reads as never answered."""
        expired: list[PendingRun] = []
        for run in tuple(self._runs.values()):
            if run.deadline > now:
                continue
            for unit_id, (host, cleanup_id) in tuple(run.open_units.items()):
                run.settle(
                    ModelHostHygieneUnitResult(
                        host=host,
                        cleanup_id=cleanup_id,
                        work_unit_id=unit_id,
                        outcome=EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE,
                        exit_code=None,
                        detail=f"no terminal for unit {unit_id}: {detail}"[:2000],
                        output_tail="",
                    )
                )
                self._by_unit.pop(unit_id, None)
            del self._runs[run.run_id]
            expired.append(run)
        return tuple(expired)


#: The store the runtime's handlers share.
PENDING_HOST_HYGIENE_RUNS = PendingHostHygieneRuns()

__all__: list[str] = [
    "DEADLINE_SLACK_SECONDS",
    "PENDING_HOST_HYGIENE_RUNS",
    "RAN",
    "PendingHostHygieneRuns",
    "PendingRun",
]
