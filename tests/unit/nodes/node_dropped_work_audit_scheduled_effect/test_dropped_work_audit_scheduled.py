# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dropped-work audit on the runtime's schedule (OMN-26205): the overlay, the hourly gate, the tick over the in-memory ledger."""

from __future__ import annotations

import ast
import json
import shlex
import sys
from collections.abc import Callable, Mapping, MutableSet
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.dropped_work_audit_scheduled_tick import (
    ModelDroppedWorkAuditTickOutcome,
    run_dropped_work_audit_tick,
)
from omnimarket.models.dropped_work_audit import (
    EnumDroppedWorkKind,
    ModelDroppedWorkAuditRules,
    ModelDroppedWorkAuditScheduleOverlay,
    ModelOwnerMissingEvent,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleJobState,
    ModelScheduleVerdictRequest,
)
from omnimarket.nodes.node_dropped_work_audit_scheduled_effect.handlers.handler_dropped_work_audit_scheduled import (
    HandlerDroppedWorkAuditScheduled,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    LedgerRouteError,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome
from omnimarket.nodes.node_schedule_verdict_compute.handlers.handler_schedule_verdict import (
    HandlerScheduleVerdict,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[4]
NODES = REPO / "src/omnimarket/nodes"
NODE = "node_dropped_work_audit_scheduled_effect"
COMPUTE = "node_dropped_work_audit_compute"
REAL_ROWS: dict[str, str] = json.loads(
    (Path(__file__).resolve().parents[1] / COMPUTE / "real_ledger_rows.json").read_text(
        encoding="utf-8"
    )
)
SIX_KIND_ROWS = (
    "ruling_standing",
    "ruling_cutover",
    "promise_sent_on_behalf",
    "terminal_blocked",
    "hold_pr_merged",
    "claim_ledger_cut",
    "msg_admit_6478",
)
T0 = datetime(2026, 11, 20, 20, 20, tzinfo=UTC)
DONE = ModelDroppedWorkAuditTickOutcome(False, "items=6 unowned=6 events=6 alarms=6")


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def example(name: str) -> dict[str, object]:
    loaded = yaml.safe_load(
        (NODES / name / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def overlay_environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(example(NODE)), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _RecordingTick:
    def __init__(self, outcome: ModelDroppedWorkAuditTickOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    def __call__(
        self,
        *,
        schedule: ModelDroppedWorkAuditScheduleOverlay,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        evented: MutableSet[str],
        publisher: object,
    ) -> ModelDroppedWorkAuditTickOutcome:
        self.calls += 1
        return self._outcome


def test_overlay_schedule(tmp_path: Path) -> None:
    """AC5: hourly through the overlay, the schedule verdict reads one run per hour, no value is a literal in code."""
    overlay = ModelDroppedWorkAuditScheduleOverlay.model_validate(example(NODE))
    rules = ModelDroppedWorkAuditRules.model_validate(example(COMPUTE))
    assert overlay.run_interval_seconds == 3600
    assert overlay.schedule_host == "dev-runtime"
    assert overlay.rules_overlay_id == COMPUTE
    assert set(rules.kinds) == set(EnumDroppedWorkKind)
    assert rules.claim_window_seconds == 21600
    assert rules.alarm_delay_seconds == 1800

    recorded = _RecordingTick(DONE)
    handler = HandlerDroppedWorkAuditScheduled(
        environ=overlay_environ(tmp_path),
        tick=recorded,
        clock=lambda: T0,
    )
    fired = [handler.handle(tick(T0 + timedelta(minutes=m))) for m in range(0, 121, 10)]
    runs = [e for e in fired if e is not None]
    assert len(runs) == 3, "one run per hour: minutes 0, 60 and 120"
    assert recorded.calls == 3
    assert {(e.job_id, e.node_name, e.outcome) for e in runs} == {
        ("dropped_work_audit", NODE, "completed")
    }

    # the schedule verdict reads the job firing while one run per hour arrives, and red once an hour and the grace pass
    verdicts = HandlerScheduleVerdict()
    last = T0 + timedelta(hours=2)

    def verdict_at(now: datetime) -> str:
        return (
            verdicts.handle(
                ModelScheduleVerdictRequest(
                    now=now,
                    observed_since=T0 - timedelta(hours=1),
                    declared=(overlay,),
                    states=(
                        ModelScheduleJobState(
                            job_id=overlay.job_id,
                            last_fired_at=last,
                            last_scheduled_for=last,
                            last_outcome="completed",
                            fire_count=3,
                            failure_count=0,
                        ),
                    ),
                )
            )
            .verdicts[0]
            .state
        )

    assert verdict_at(last + timedelta(minutes=59)) == "FIRING"
    assert (
        verdict_at(last + timedelta(hours=1, seconds=overlay.grace_seconds + 1))
        == "OVERDUE"
    )


def test_a_failed_tick_is_a_failed_run_event_that_names_it(tmp_path: Path) -> None:
    failed = ModelDroppedWorkAuditTickOutcome(
        True, "the ledger read of CLAIM failed: ledger-file-unreadable:OSError"
    )
    handler = HandlerDroppedWorkAuditScheduled(
        environ=overlay_environ(tmp_path),
        tick=_RecordingTick(failed),
        clock=lambda: T0,
    )
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == failed.detail


def test_an_unbound_route_is_a_failed_run_event(tmp_path: Path) -> None:
    def unbound(**_: object) -> ModelDroppedWorkAuditTickOutcome:
        raise LedgerRouteError("ONEX_LEDGER_BUS_READ_COMMAND is not set")

    handler = HandlerDroppedWorkAuditScheduled(
        environ=overlay_environ(tmp_path), tick=unbound, clock=lambda: T0
    )
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "ONEX_LEDGER_BUS_READ_COMMAND" in event.detail


def test_without_an_overlay_there_is_no_run(tmp_path: Path) -> None:
    handler = HandlerDroppedWorkAuditScheduled(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
        tick=_RecordingTick(DONE),
        clock=lambda: T0,
    )
    assert handler.handle(tick(T0)) is None


class _Appender:
    def __init__(self, ledger: InMemoryLedger) -> None:
        self.rows: list[tuple[str, str]] = []
        self._ledger = ledger

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.rows.append((rows, request_id))
        self._ledger.lines.extend(rows.splitlines())
        return ModelAppendOutcome(status="accepted")


class _Publisher:
    def __init__(self) -> None:
        self.events: list[ModelOwnerMissingEvent] = []

    def publish(self, event: ModelOwnerMissingEvent) -> bool:
        self.events.append(event)
        return True


def tick_environ(
    tmp_path: Path,
) -> tuple[dict[str, str], InMemoryLedger, ModelDroppedWorkAuditScheduleOverlay]:
    ledger = InMemoryLedger([REAL_ROWS[name] for name in SIX_KIND_ROWS])
    pr_state = tmp_path / "pr-watcher-state.json"
    pr_state.write_text(
        json.dumps(
            {"merges": {"omnibase_infra#6095": {"merged_at": "2026-11-19T23:55:34Z"}}}
        ),
        encoding="utf-8",
    )
    refusals = json.dumps(
        [
            {
                "candidate": "omnimarket#6478",
                "ticket": "OMN-20540",
                "reason": "declares a ticket under no goal root",
                "observed_at": "2026-11-20T13:49:00Z",
            }
        ]
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('print(' + repr(refusals) + ')')}"
    rules_dir = tmp_path / "overlays" / COMPUTE
    rules_dir.mkdir(parents=True)
    (rules_dir / "overlay.yaml").write_text(
        yaml.safe_dump(example(COMPUTE)), encoding="utf-8"
    )
    overlay = ModelDroppedWorkAuditScheduleOverlay.model_validate(example(NODE))
    environ = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
        "ONEX_PR_WATCHER_STATE": str(pr_state),
        "ONEX_ADMISSION_REFUSAL_READ_COMMAND": command,
    }
    return environ, ledger, overlay


def test_tick_over_the_in_memory_ledger_raises_each_item_once(tmp_path: Path) -> None:
    environ, ledger, overlay = tick_environ(tmp_path)
    appender = _Appender(ledger)
    publisher = _Publisher()
    evented: set[str] = set()
    first = run_dropped_work_audit_tick(
        schedule=overlay,
        environ=environ,
        clock=lambda: T0,
        evented=evented,
        source=ledger,
        appender=appender,
        publisher=publisher,
    )
    assert not first.failed, first.detail
    assert first.result is not None
    assert first.result.unowned == 6
    assert len(publisher.events) == 6
    assert len(appender.rows) == 6
    # the rerun over the ledger that now holds the six alarm rows writes nothing more
    second = run_dropped_work_audit_tick(
        schedule=overlay,
        environ=environ,
        clock=lambda: T0 + timedelta(hours=1),
        evented=evented,
        source=ledger,
        appender=appender,
        publisher=publisher,
    )
    assert not second.failed, second.detail
    assert second.result is not None
    assert second.result.unowned == 6
    assert len(publisher.events) == 6
    assert len(appender.rows) == 6


def test_dry_run_appends_and_publishes_nothing(tmp_path: Path) -> None:
    environ, ledger, overlay = tick_environ(tmp_path)
    appender = _Appender(ledger)
    publisher = _Publisher()
    outcome = run_dropped_work_audit_tick(
        schedule=overlay,
        environ=environ,
        clock=lambda: T0,
        evented=set(),
        source=ledger,
        appender=appender,
        publisher=publisher,
        dry_run=True,
    )
    assert not outcome.failed
    assert outcome.result is not None
    assert len(outcome.result.alarms) == 6
    assert appender.rows == []
    assert publisher.events == []


def test_an_unreadable_pull_request_state_fails_the_run_by_name(tmp_path: Path) -> None:
    environ, ledger, overlay = tick_environ(tmp_path)
    environ["ONEX_PR_WATCHER_STATE"] = str(tmp_path / "absent.json")
    outcome = run_dropped_work_audit_tick(
        schedule=overlay,
        environ=environ,
        clock=lambda: T0,
        evented=set(),
        source=ledger,
        appender=_Appender(ledger),
        publisher=_Publisher(),
    )
    assert outcome.failed
    assert "pull request state file is unreadable" in outcome.detail


def test_no_schedule_value_is_a_literal_in_code() -> None:
    for path in (
        NODES / NODE / "handlers/handler_dropped_work_audit_scheduled.py",
        REPO / "src/omnimarket/handlers/dropped_work_audit_scheduled_tick.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = {
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
        }
        assert literals & {3600, 1800, 21600, "dev-runtime", "owner-missing"} == set()
