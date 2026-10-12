# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 2026-10-09 replay: a day of run events for the six silent failures.

The timeline is the seam's own events (run observed, heartbeat) with the times
of the plan's evidence table. A small stand-in fold keeps rows shaped like the
liveness projection's state row and run history, and every evaluation reads
them through ``facts_from_projection``, so the replay exercises the adapter the
effect will use over the real fold.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEmitter,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
    ModelAutomationHeartbeat,
    ModelAutomationLivenessOverlay,
    ModelAutomationRunObserved,
    load_automation_liveness_overlay,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.adapter_projection_rows import (
    KEPT_RUNS,
    facts_from_projection,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelObserverFacts,
    ModelTriggerExpectation,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import Driver

OVERLAY_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures/automation_liveness/replay_2026_10_09/overlay.yaml"
)
DECLARED_AT = datetime(2026, 10, 8, 22, 0, tzinfo=UTC)
REPLAY_START = datetime(2026, 10, 8, 22, 0, tzinfo=UTC)
REPLAY_END = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)

SLOT_FILLER = "host-a/slot-filler"
LANE_RUNNER = "host-a/lane-runner"
TICK = "session/orchestrator-tick"
DEV_RUNTIME = "host-b/dev-runtime"
REDEPLOY = "host-b/dev-redeploy"

_OK = EnumAutomationRunOutcome.OK
_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason


def _t(hhmm: str, day: int = 9) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


@dataclass(frozen=True)
class ReplayCase:
    """One silent failure: when it began and the verdict it must raise."""

    process_id: str
    fault_start: datetime
    verdict: EnumAutomationLivenessVerdict
    reason: EnumAutomationLivenessReason
    evidence: str


CASES: tuple[ReplayCase, ...] = (
    ReplayCase(
        SLOT_FILLER,
        _t("02:10"),
        _V.IDLE_WITH_DEMAND,
        _R.CONSECUTIVE_IDLE_RUNS_WITH_DEMAND,
        "dispatched lanes and placed 0, 02:10Z to 03:50Z",
    ),
    ReplayCase(
        LANE_RUNNER,
        _t("05:00"),
        _V.FAILED,
        _R.FAILURE_WINDOW,
        "every lane refused, 05:00Z to 05:43Z",
    ),
    ReplayCase(
        TICK,
        _t("23:42", day=8),
        _V.MISSED,
        _R.NO_RUN_IN_MAX_SILENCE,
        "session ticks stopped, 23:42Z to 06:01Z",
    ),
    ReplayCase(
        DEV_RUNTIME,
        _t("10:16"),
        _V.OVERRUN,
        _R.PROGRESS_STALLED_WITH_DEMAND,
        "dev runtime crash-looped with work waiting, 10:16Z to 14:10Z",
    ),
    ReplayCase(
        REDEPLOY,
        _t("11:59"),
        _V.TRIGGER_UNANSWERED,
        _R.ANSWER_PAST_DEADLINE,
        "the merge carrying the fix was not redeployed, 11:59Z to 13:49Z",
    ),
    ReplayCase(
        SLOT_FILLER,
        _t("13:50"),
        _V.UNOBSERVABLE,
        _R.DEMAND_UNREADABLE,
        "every candidate tier read unreadable, from 13:50Z",
    ),
)


# --------------------------------------------------------------------------- timeline


def _digest(process_id: str) -> str:
    return hashlib.sha256(process_id.encode()).hexdigest()


def _run(
    process_id: str,
    host: str,
    started: datetime,
    *,
    seconds: int = 30,
    outcome: EnumAutomationRunOutcome = _OK,
    work: int = 1,
    demand: int | None = 1,
) -> ModelAutomationRunObserved:
    finished = started + timedelta(seconds=seconds)
    return ModelAutomationRunObserved(
        process_id=process_id,
        host=host,
        run_id=f"{process_id}:{started.isoformat()}",
        phase=EnumAutomationRunPhase.FINISHED,
        started_at=started,
        finished_at=finished,
        outcome=outcome,
        exit_code=0 if outcome is _OK else 70,
        did_work_count=work,
        demand_count=demand,
        evidence_ref=f"replay:{process_id}",
        emitter=EnumAutomationEmitter.OBSERVER,
        observed_at=finished + timedelta(seconds=60),
        contract_digest=_digest(process_id),
    )


def _every(start: datetime, end: datetime, seconds: int) -> list[datetime]:
    out, t = [], start
    while t < end:
        out.append(t)
        t += timedelta(seconds=seconds)
    return out


def timeline() -> list[ModelAutomationRunObserved | ModelAutomationHeartbeat]:
    """Every event of the replay day, in observed order."""
    events: list[ModelAutomationRunObserved | ModelAutomationHeartbeat] = []
    for t in _every(REPLAY_START, REPLAY_END, 600):
        if _t("02:10") <= t <= _t("03:50"):
            events.append(_run(SLOT_FILLER, "host-a", t, work=0, demand=11))
        elif t >= _t("13:50"):
            events.append(_run(SLOT_FILLER, "host-a", t, work=0, demand=None))
        else:
            events.append(_run(SLOT_FILLER, "host-a", t, work=2, demand=3))
    for t in _every(REPLAY_START, REPLAY_END, 300):
        refused = _t("05:00") <= t <= _t("05:40")
        events.append(
            _run(
                LANE_RUNNER,
                "host-a",
                t,
                seconds=20,
                outcome=EnumAutomationRunOutcome.REFUSED if refused else _OK,
                work=0 if refused else 1,
                demand=28 if refused else 1,
            )
        )
    for t in _every(_t("22:12", day=8), REPLAY_END, 1800):
        if _t("23:42", day=8) < t < _t("06:01"):
            continue
        events.append(_run(TICK, "session", t))
    events.append(_run(TICK, "session", _t("06:01")))
    progress = 0
    stalled_since = _t("10:16")
    for t in _every(REPLAY_START, REPLAY_END, 60):
        stalled = stalled_since <= t < _t("14:10")
        if not stalled:
            progress += 5
        events.append(
            ModelAutomationHeartbeat(
                process_id=DEV_RUNTIME,
                host="host-b",
                process_started_at=REPLAY_START,
                progress_counter=progress,
                last_progress_at=stalled_since if stalled else t,
                demand_count=40 if stalled else 2,
                emitted_at=t,
            )
        )
    for answered in (_t("09:42"), _t("13:49")):
        events.append(
            _run(REDEPLOY, "host-b", answered - timedelta(minutes=10), seconds=600)
        )

    def observed(e: ModelAutomationRunObserved | ModelAutomationHeartbeat) -> datetime:
        return (
            e.observed_at if isinstance(e, ModelAutomationRunObserved) else e.emitted_at
        )

    return sorted(events, key=observed)


EXPECTATIONS: tuple[tuple[datetime, datetime], ...] = (
    (_t("09:30"), _t("09:42")),
    (_t("11:59"), _t("13:49")),
)


# --------------------------------------------------------------------------- fold


@dataclass(frozen=True)
class StateRow:
    """Stand-in with the projection state row's field names."""

    process_id: str
    host: str
    declared_at: datetime | None = None
    process_state: EnumAutomationProcessState | None = None
    last_run_at: datetime | None = None
    last_demand_count: int | None = None
    last_heartbeat_at: datetime | None = None
    last_progress_at: datetime | None = None
    progress_counter: int | None = None
    open_run_started_at: datetime | None = None
    verdict: EnumAutomationLivenessVerdict | None = None
    verdict_reason: EnumAutomationLivenessReason | None = None
    verdict_evaluated_at: datetime | None = None


@dataclass(frozen=True)
class RunRow:
    """Stand-in with the projection run-history row's field names."""

    process_id: str
    host: str
    run_id: str
    phase: EnumAutomationRunPhase
    started_at: datetime
    finished_at: datetime | None
    outcome: EnumAutomationRunOutcome | None
    did_work_count: int | None
    demand_count: int | None
    unseen_runs: int


@dataclass
class StandInFold:
    """Applies events in order, like the projection fold, keeping recent runs."""

    overlay: ModelAutomationLivenessOverlay
    rows: dict[tuple[str, str], StateRow] = field(default_factory=dict)
    runs: dict[tuple[str, str], deque[RunRow]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for e in self.overlay.processes:
            key = (e.process_id, e.host)
            self.rows[key] = StateRow(
                e.process_id, e.host, declared_at=DECLARED_AT, process_state=e.state
            )
            self.runs[key] = deque(maxlen=KEPT_RUNS)

    def apply(
        self, event: ModelAutomationRunObserved | ModelAutomationHeartbeat
    ) -> None:
        key = (event.process_id, event.host)
        row = self.rows[key]
        if isinstance(event, ModelAutomationHeartbeat):
            self.rows[key] = replace(
                row,
                last_heartbeat_at=event.emitted_at,
                last_progress_at=event.last_progress_at,
                progress_counter=event.progress_counter,
                last_demand_count=event.demand_count,
            )
            return
        self.runs[key].append(
            RunRow(
                event.process_id,
                event.host,
                event.run_id,
                event.phase,
                event.started_at,
                event.finished_at,
                event.outcome,
                event.did_work_count,
                event.demand_count,
                event.unseen_runs,
            )
        )
        self.rows[key] = replace(
            row,
            last_run_at=event.finished_at or event.started_at,
            last_demand_count=event.demand_count,
        )


# --------------------------------------------------------------------------- replay


def replay(step_seconds: int = 60) -> Driver:
    """Evaluate the whole replay day once a minute; the driver keeps every result."""
    overlay = load_automation_liveness_overlay(OVERLAY_PATH)
    driver = Driver(overlay)
    fold = StandInFold(overlay)
    events = timeline()
    cursor = 0
    now = REPLAY_START
    while now <= REPLAY_END:
        while cursor < len(events):
            e = events[cursor]
            seen = (
                e.observed_at
                if isinstance(e, ModelAutomationRunObserved)
                else e.emitted_at
            )
            if seen > now:
                break
            fold.apply(e)
            cursor += 1
        processes = tuple(
            facts_from_projection(fold.rows[k], fold.runs[k]) for k in fold.rows
        )
        expectations = tuple(
            ModelTriggerExpectation(
                process_id=REDEPLOY,
                host="host-b",
                match_key=f"merge@{trigger.isoformat()}",
                triggered_at=trigger,
                answered_at=answer if answer <= now else None,
            )
            for trigger, answer in EXPECTATIONS
            if trigger <= now
        )
        observers = tuple(
            ModelObserverFacts(
                host=host,
                observer_process_id=f"{host}/observer",
                expected_since=DECLARED_AT,
                last_heartbeat_at=now - timedelta(seconds=30),
            )
            for host in ("host-a", "host-b")
        )
        driver.tick(
            now, processes=processes, expectations=expectations, observers=observers
        )
        now += timedelta(seconds=step_seconds)
    return driver


def summary(driver: Driver) -> list[str]:
    """One line per alarm raised and cleared over the replay day."""
    lines = [
        f"evaluations={len(driver.results)} void={sum(r.void for r in driver.results)} "
        f"raised={len(driver.raised)} cleared={len(driver.cleared)}"
    ]
    for r in driver.raised:
        lines.append(
            f"raised {r.opened_at:%m-%dT%H:%MZ} {r.process_id} {r.verdict.value}/"
            f"{r.reason.value} {r.severity.value} due {r.delivery_due_at:%H:%MZ}"
        )
    for c in driver.cleared:
        lines.append(
            f"cleared {c.cleared_at:%m-%dT%H:%MZ} {c.process_id} {c.verdict.value}"
        )
    return lines


if __name__ == "__main__":
    for line in summary(replay()):
        print(line)
