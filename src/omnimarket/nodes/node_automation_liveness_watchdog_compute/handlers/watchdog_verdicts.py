# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One process's verdict from its overlay entry and its folded facts.

The rules run in a fixed order, and the order is the design: a process whose
evidence cannot be read has no other verdict worth stating, a disabled
schedule is MISSED whatever its last run said, a hung run outranks the silence
it causes, and only a process that is running on time is judged on what its
runs did. The planted self-test calls this same function, so a defect here
voids the evaluation instead of passing through it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from omnimarket.models.liveness.model_automation_liveness import (
    DAEMON_TRIGGER_KINDS,
    FAILING_OUTCOMES,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunOutcome,
    ModelAutomationLivenessEntry,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    EXTERNAL_DEADMAN_SILENCE_SECONDS,
    FAILURE_WINDOW_RUNS,
    FAILURE_WINDOW_THRESHOLD,
    NOT_INSTALLED_GRACE_SECONDS,
    SLOW_CADENCE_SECONDS,
    ModelProcessRunFacts,
    ModelRunFact,
    ModelTriggerExpectation,
)

_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason

_RUN_REASON: dict[EnumAutomationRunOutcome, EnumAutomationLivenessReason] = {
    EnumAutomationRunOutcome.FAILED: _R.RUN_FAILED,
    EnumAutomationRunOutcome.DEGRADED: _R.RUN_DEGRADED,
    EnumAutomationRunOutcome.REFUSED: _R.RUN_REFUSED,
    EnumAutomationRunOutcome.TIMEOUT: _R.RUN_TIMEOUT,
}


@dataclass(frozen=True)
class RawVerdict:
    """A verdict before the confirmation hold and the folding."""

    verdict: EnumAutomationLivenessVerdict
    reason: EnumAutomationLivenessReason
    detail: str = ""


def _age(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds()


def _latest(*instants: datetime | None) -> datetime | None:
    present = [t for t in instants if t is not None]
    return max(present) if present else None


def _last_activity(facts: ModelProcessRunFacts) -> datetime | None:
    newest = facts.newest_run
    return _latest(
        facts.last_run_at,
        newest.finished_at if newest is not None else None,
        facts.open_run_started_at,
        facts.last_heartbeat_at,
    )


def failure_window(
    entry: ModelAutomationLivenessEntry, runs: tuple[ModelRunFact, ...]
) -> bool:
    """Two or more failures in the last six runs, or any failure when slow.

    For a cadence of six hours or slower one failure already is the window,
    judged on the newest run, so a recovered daily job is not held FAILED for
    a week by a failure six runs back.
    """
    if not runs:
        return False
    interval = entry.expected_interval_seconds
    if interval is not None and interval >= SLOW_CADENCE_SECONDS:
        return runs[-1].outcome in FAILING_OUTCOMES
    recent = runs[-FAILURE_WINDOW_RUNS:]
    return (
        sum(1 for r in recent if r.outcome in FAILING_OUTCOMES)
        >= FAILURE_WINDOW_THRESHOLD
    )


def _never_seen(facts: ModelProcessRunFacts) -> bool:
    return not facts.runs and _last_activity(facts) is None


def _fresh_observer_report(
    facts: ModelProcessRunFacts,
) -> tuple[EnumAutomationLivenessVerdict, EnumAutomationLivenessReason] | None:
    """The observer's own report, unless a later run or heartbeat superseded it.

    An observer reports an unreadable source or a disabled schedule; it does
    not always report the recovery. Activity seen after the report is the
    recovery, so the report stops counting then.
    """
    report = facts.observer_report
    if report is None:
        return None
    activity = _last_activity(facts)
    if activity is not None and activity > report.reported_at:
        return None
    return report.verdict, report.reason


def evaluate_process(
    entry: ModelAutomationLivenessEntry,
    facts: ModelProcessRunFacts | None,
    now: datetime,
    expectations: Iterable[ModelTriggerExpectation] = (),
    *,
    external_deadman: bool = False,
) -> RawVerdict:
    """The verdict of one declared process at ``now``."""
    if facts is None:
        facts = ModelProcessRunFacts(process_id=entry.process_id, host=entry.host)
    declared = facts.declared_at or now

    if entry.state is EnumAutomationProcessState.NOT_INSTALLED and _never_seen(facts):
        if _age(now, declared) <= NOT_INSTALLED_GRACE_SECONDS:
            return RawVerdict(_V.NOT_READY, _R.NOT_INSTALLED_IN_GRACE)
        return RawVerdict(_V.MISSED, _R.NOT_INSTALLED_PAST_GRACE)

    report = _fresh_observer_report(facts)
    if report is not None and report[0] is _V.UNOBSERVABLE:
        return RawVerdict(report[0], report[1], "reported by the observer")
    newest = facts.newest_run
    if newest is not None and newest.unseen_runs > 0:
        return RawVerdict(
            _V.UNOBSERVABLE,
            _R.UNSEEN_RUNS,
            f"{newest.unseen_runs} runs between two reads of latest-only evidence",
        )
    daemon = entry.trigger.kind in DAEMON_TRIGGER_KINDS
    if entry.declares_demand:
        if daemon and facts.last_heartbeat_at is not None:
            if facts.heartbeat_demand_count is None:
                return RawVerdict(_V.UNOBSERVABLE, _R.DEMAND_UNREADABLE)
        elif newest is not None and newest.demand_count is None:
            return RawVerdict(
                _V.UNOBSERVABLE,
                _R.DEMAND_UNREADABLE,
                f"run {newest.run_id} carried no demand count",
            )
    if report is not None:
        return RawVerdict(report[0], report[1], "reported by the observer")

    if external_deadman:
        completed = newest.finished_at if newest is not None else None
        since = completed or declared
        if _age(now, since) >= EXTERNAL_DEADMAN_SILENCE_SECONDS:
            return RawVerdict(
                _V.EXTERNAL_DEADMAN_SILENT,
                _R.EXTERNAL_RUN_MISSING,
                f"no completed run since {since.isoformat()}",
            )

    if entry.correlation is not None:
        deadline = entry.correlation.answer_deadline_seconds
        late = sorted(
            (
                e
                for e in expectations
                if e.answered_at is None and _age(now, e.triggered_at) >= deadline
            ),
            key=lambda e: e.triggered_at,
        )
        if late:
            oldest = late[0]
            waited = int(_age(now, oldest.triggered_at) // 60)
            text = (
                f"{oldest.match_key} unanswered since "
                f"{oldest.triggered_at.isoformat()} ({waited} min)"
            )
            if len(late) > 1:
                text += f"; {len(late) - 1} more unanswered"
            if oldest.detail:
                text += f"; last decision: {oldest.detail}"
            return RawVerdict(_V.TRIGGER_UNANSWERED, _R.ANSWER_PAST_DEADLINE, text)

    if (
        facts.open_run_started_at is not None
        and entry.max_runtime_seconds is not None
        and _age(now, facts.open_run_started_at) >= entry.max_runtime_seconds
    ):
        return RawVerdict(
            _V.OVERRUN,
            _R.RUN_OPEN_PAST_MAX_RUNTIME,
            f"run open since {facts.open_run_started_at.isoformat()}",
        )

    silence = entry.max_silence_seconds
    if daemon and silence is not None:
        if _age(now, facts.last_heartbeat_at or declared) > silence:
            return RawVerdict(_V.MISSED, _R.NO_RUN_IN_MAX_SILENCE, "no heartbeat")
        if _age(now, facts.last_progress_at or declared) > silence:
            if not entry.declares_demand:
                return RawVerdict(_V.MISSED, _R.NO_RUN_IN_MAX_SILENCE, "no progress")
            demand = facts.heartbeat_demand_count or 0
            if demand > 0:
                return RawVerdict(
                    _V.OVERRUN,
                    _R.PROGRESS_STALLED_WITH_DEMAND,
                    f"progress counter still at {facts.progress_counter} "
                    f"with demand {demand}",
                )
            return RawVerdict(_V.NO_DEMAND, _R.DEMAND_READ_ZERO)
        return RawVerdict(_V.HEALTHY, _R.INSIDE_BOUNDS)

    if silence is not None:
        last = _last_activity(facts) or declared
        if _age(now, last) > silence:
            return RawVerdict(
                _V.MISSED, _R.NO_RUN_IN_MAX_SILENCE, f"last run {last.isoformat()}"
            )

    if newest is None:
        return RawVerdict(_V.HEALTHY, _R.INSIDE_BOUNDS)
    if failure_window(entry, facts.runs):
        return RawVerdict(_V.FAILED, _R.FAILURE_WINDOW)
    if newest.outcome in FAILING_OUTCOMES:
        return RawVerdict(
            _V.FAILED, _RUN_REASON[newest.outcome], f"run {newest.run_id}"
        )
    if entry.declares_demand:
        tail = facts.runs[-entry.idle_runs_before_alarm :]
        if len(tail) == entry.idle_runs_before_alarm and all(
            r.outcome is EnumAutomationRunOutcome.OK
            and r.did_work_count == 0
            and (r.demand_count or 0) > 0
            for r in tail
        ):
            return RawVerdict(
                _V.IDLE_WITH_DEMAND,
                _R.CONSECUTIVE_IDLE_RUNS_WITH_DEMAND,
                f"{len(tail)} runs did no work with demand {newest.demand_count}",
            )
        if newest.did_work_count == 0 and newest.demand_count == 0:
            return RawVerdict(_V.NO_DEMAND, _R.DEMAND_READ_ZERO)
    return RawVerdict(_V.HEALTHY, _R.INSIDE_BOUNDS)


__all__ = ["RawVerdict", "evaluate_process", "failure_window"]
