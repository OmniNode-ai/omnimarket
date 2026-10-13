# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The planted contracts every evaluation checks before it trusts itself.

Five contracts with known facts and known answers -- one MISSED, one FAILED by
alternation, one IDLE_WITH_DEMAND, one UNOBSERVABLE, one HEALTHY -- are
evaluated by the same ``evaluate_process`` the real processes go through. If
any answer differs, the evaluation is void. The facts are built relative to the
evaluation time, so the planted answers hold at every tick.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEmitter,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunOutcome,
    EnumAutomationRunRecord,
    EnumAutomationTriggerKind,
    EnumPositiveControlKind,
    ModelAutomationLivenessEntry,
    ModelAutomationPositiveControl,
    ModelAutomationTrigger,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.watchdog_verdicts import (
    RawVerdict,
    evaluate_process,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelObserverReport,
    ModelPlantedCase,
    ModelPlantedOutcome,
    ModelProcessRunFacts,
    ModelRunFact,
)

PLANTED_HOST = "watchdog-selftest"
_INTERVAL = 600
_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason
_OK = EnumAutomationRunOutcome.OK
_FAILED = EnumAutomationRunOutcome.FAILED


def _entry(name: str) -> ModelAutomationLivenessEntry:
    return ModelAutomationLivenessEntry(
        process_id=f"watchdog-selftest/{name}",
        owner_repo="omnimarket",
        host=PLANTED_HOST,
        trigger=ModelAutomationTrigger(
            kind=EnumAutomationTriggerKind.LAUNCHD_INTERVAL,
            native_id=f"watchdog-selftest.{name}",
        ),
        expected_interval_seconds=_INTERVAL,
        alarm_deadline_seconds=86400,
        max_runtime_seconds=_INTERVAL // 2,
        emitter=EnumAutomationEmitter.SELF,
        run_record=EnumAutomationRunRecord.PER_RUN,
        real_work="work_count",
        demand="demand_count > 0",
        positive_control=ModelAutomationPositiveControl(
            kind=EnumPositiveControlKind.CLASS_DRILL,
            ref="watchdog planted contract",
        ),
    )


def _runs(
    now: datetime,
    outcomes: list[tuple[EnumAutomationRunOutcome, int, int]],
    *,
    newest_finished_ago: int = 60,
) -> tuple[ModelRunFact, ...]:
    """Runs one interval apart, the newest finished ``newest_finished_ago`` s ago."""
    runs = []
    count = len(outcomes)
    for i, (outcome, work, demand) in enumerate(outcomes):
        finished = now - timedelta(
            seconds=newest_finished_ago + (count - 1 - i) * _INTERVAL
        )
        runs.append(
            ModelRunFact(
                run_id=f"planted-{i}",
                started_at=finished - timedelta(seconds=30),
                finished_at=finished,
                outcome=outcome,
                did_work_count=work,
                demand_count=demand,
            )
        )
    return tuple(runs)


def _case(
    name: str,
    now: datetime,
    expected: tuple[EnumAutomationLivenessVerdict, EnumAutomationLivenessReason],
    runs: tuple[ModelRunFact, ...],
    report: ModelObserverReport | None = None,
) -> ModelPlantedCase:
    entry = _entry(name)
    facts = ModelProcessRunFacts(
        process_id=entry.process_id,
        host=entry.host,
        declared_at=now - timedelta(days=1),
        runs=runs,
        last_run_at=runs[-1].finished_at if runs else None,
        observer_report=report,
    )
    return ModelPlantedCase(
        name=name,
        entry=entry,
        facts=facts,
        expected_verdict=expected[0],
        expected_reason=expected[1],
    )


def default_planted_cases(now: datetime) -> tuple[ModelPlantedCase, ...]:
    """The five planted contracts of 4.9, with facts relative to ``now``."""
    return (
        _case(
            "missed",
            now,
            (_V.MISSED, _R.NO_RUN_IN_MAX_SILENCE),
            _runs(now, [(_OK, 1, 1)], newest_finished_ago=3 * 60 * 60),
        ),
        _case(
            "failed-alternating",
            now,
            (_V.FAILED, _R.FAILURE_WINDOW),
            _runs(now, [(_FAILED, 0, 1), (_OK, 1, 1)] * 3),
        ),
        _case(
            "idle-with-demand",
            now,
            (_V.IDLE_WITH_DEMAND, _R.CONSECUTIVE_IDLE_RUNS_WITH_DEMAND),
            _runs(now, [(_OK, 0, 4)] * 3),
        ),
        _case(
            "unobservable",
            now,
            (_V.UNOBSERVABLE, _R.EVIDENCE_UNREADABLE),
            _runs(now, [(_OK, 1, 1)], newest_finished_ago=600),
            ModelObserverReport(
                verdict=_V.UNOBSERVABLE,
                reason=_R.EVIDENCE_UNREADABLE,
                reported_at=now - timedelta(seconds=60),
            ),
        ),
        _case(
            "healthy",
            now,
            (_V.HEALTHY, _R.INSIDE_BOUNDS),
            _runs(now, [(_OK, 2, 2)] * 3),
        ),
    )


Evaluator = Callable[..., RawVerdict]


def run_selftest(
    cases: tuple[ModelPlantedCase, ...],
    now: datetime,
    evaluator: Evaluator = evaluate_process,
) -> tuple[ModelPlantedOutcome, ...]:
    """Each planted contract's expected and actual verdict."""
    outcomes = []
    for case in cases:
        raw = evaluator(case.entry, case.facts, now)
        outcomes.append(
            ModelPlantedOutcome(
                name=case.name,
                expected_verdict=case.expected_verdict,
                expected_reason=case.expected_reason,
                actual_verdict=raw.verdict,
                actual_reason=raw.reason,
            )
        )
    return tuple(outcomes)


__all__ = ["PLANTED_HOST", "default_planted_cases", "run_selftest"]
