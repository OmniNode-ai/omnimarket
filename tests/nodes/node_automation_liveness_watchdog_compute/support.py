# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders and a ticking driver for the watchdog compute tests (OMN-20806)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEmitter,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunOutcome,
    EnumAutomationRunRecord,
    EnumAutomationTriggerKind,
    EnumPositiveControlKind,
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmRaised,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessOverlay,
    ModelAutomationPositiveControl,
    ModelAutomationTrigger,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.handler_automation_liveness_watchdog import (
    HandlerAutomationLivenessWatchdog,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelAutomationLivenessWatchdogRequest,
    ModelAutomationLivenessWatchdogResult,
    ModelObserverFacts,
    ModelProcessRunFacts,
    ModelProcessVerdict,
    ModelRunFact,
    ModelWatchdogState,
)

T0 = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)
WATCHDOG = "watchdog/primary"
WATCHDOG_HOST = "host-c"


def at(text: str) -> datetime:
    """An instant from ``YYYY-MM-DDTHH:MM[:SS]`` in UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def entry(
    process_id: str,
    *,
    host: str = "host-a",
    kind: EnumAutomationTriggerKind = EnumAutomationTriggerKind.LAUNCHD_INTERVAL,
    interval: int | None = 600,
    max_runtime: int | None = 300,
    deadline: int = 86400,
    demand: str = "eligible > 0",
    emitter: EnumAutomationEmitter = EnumAutomationEmitter.SELF,
    **extra: Any,
) -> ModelAutomationLivenessEntry:
    data: dict[str, Any] = {
        "process_id": process_id,
        "owner_repo": "example-owner/example-repo",
        "host": host,
        "trigger": ModelAutomationTrigger(kind=kind, native_id=f"{process_id}.unit"),
        "expected_interval_seconds": interval,
        "alarm_deadline_seconds": deadline,
        "max_runtime_seconds": max_runtime,
        "emitter": emitter,
        "run_record": EnumAutomationRunRecord.PER_RUN,
        "real_work": "work_count",
        "demand": demand,
        "positive_control": ModelAutomationPositiveControl(
            kind=EnumPositiveControlKind.CLASS_DRILL, ref="test drill"
        ),
    }
    if emitter is EnumAutomationEmitter.OBSERVER:
        data["evidence"] = {"source": "receipts_file", "locator": "receipts.jsonl"}
    data.update(extra)
    return ModelAutomationLivenessEntry.model_validate(data)


def overlay(*entries: ModelAutomationLivenessEntry) -> ModelAutomationLivenessOverlay:
    return ModelAutomationLivenessOverlay(
        schema_version="automation-liveness-overlay/v1", processes=entries
    )


def run(
    run_id: str,
    finished_at: datetime,
    outcome: EnumAutomationRunOutcome = EnumAutomationRunOutcome.OK,
    *,
    work: int = 1,
    demand: int | None = 1,
    duration: int = 30,
    unseen: int = 0,
) -> ModelRunFact:
    return ModelRunFact(
        run_id=run_id,
        started_at=finished_at - timedelta(seconds=duration),
        finished_at=finished_at,
        outcome=outcome,
        did_work_count=work,
        demand_count=demand,
        unseen_runs=unseen,
    )


def facts(
    e: ModelAutomationLivenessEntry,
    runs: Sequence[ModelRunFact] = (),
    *,
    declared_at: datetime | None = None,
    **extra: Any,
) -> ModelProcessRunFacts:
    return ModelProcessRunFacts(
        process_id=e.process_id,
        host=e.host,
        declared_at=declared_at or T0 - timedelta(days=1),
        runs=tuple(runs),
        last_run_at=runs[-1].finished_at if runs else None,
        **extra,
    )


def observer(host: str, last: datetime | None) -> ModelObserverFacts:
    return ModelObserverFacts(
        host=host,
        observer_process_id=f"{host}/observer",
        expected_since=T0 - timedelta(days=1),
        last_heartbeat_at=last,
    )


@dataclass
class Driver:
    """Ticks the handler, carrying the state like the effect will."""

    overlay: ModelAutomationLivenessOverlay
    handler: HandlerAutomationLivenessWatchdog = field(
        default_factory=HandlerAutomationLivenessWatchdog
    )
    state: ModelWatchdogState = field(default_factory=ModelWatchdogState)
    raised: list[ModelAutomationAlarmRaised] = field(default_factory=list)
    cleared: list[ModelAutomationAlarmCleared] = field(default_factory=list)
    results: list[ModelAutomationLivenessWatchdogResult] = field(default_factory=list)

    def tick(
        self, now: datetime, **request: Any
    ) -> ModelAutomationLivenessWatchdogResult:
        result = self.handler.handle(
            ModelAutomationLivenessWatchdogRequest(
                evaluated_at=now,
                overlay=self.overlay,
                watchdog_process_id=WATCHDOG,
                watchdog_host=WATCHDOG_HOST,
                state=self.state,
                **request,
            )
        )
        self.state = result.state
        self.raised.extend(result.alarms_raised)
        self.cleared.extend(result.alarms_cleared)
        self.results.append(result)
        return result


def verdict_of(
    result: ModelAutomationLivenessWatchdogResult, process_id: str
) -> ModelProcessVerdict:
    return next(v for v in result.verdicts if v.process_id == process_id)


def raised_for(
    raised: Iterable[ModelAutomationAlarmRaised],
    process_id: str,
    verdict: EnumAutomationLivenessVerdict | None = None,
) -> list[ModelAutomationAlarmRaised]:
    return [
        r
        for r in raised
        if r.process_id == process_id and (verdict is None or r.verdict is verdict)
    ]


def minutes(start: datetime, count: int, step: int = 60) -> list[datetime]:
    return [start + timedelta(seconds=i * step) for i in range(count)]
