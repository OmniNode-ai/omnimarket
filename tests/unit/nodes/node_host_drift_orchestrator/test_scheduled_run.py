# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-tick route of the host drift orchestrator (OMN-24197): golden, silent host, duplicate tick and stopped run.

The handler is built the way the runtime builds it, with the real schedule gate over the in-memory profile overlay and an
in-memory lab-work sender; only the sender's answers are scripted.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_host_drift_orchestrator import (
    EnumHostDriftHostVerdict,
    EnumHostDriftRunOutcome,
    ModelHostDriftOrchestratorOverlay,
    ModelHostDriftRunCompleted,
)
from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers import (
    handler_host_drift_scheduled as scheduled_module,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.contract_config import (
    run_completed_topic,
    schedule_run_topic,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.handler_host_drift_scheduled import (
    NODE_NAME,
    HandlerHostDriftScheduled,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.sender_factory import (
    build_sender,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.sender_in_memory import (
    InMemoryHostDriftSender,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
T0 = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC)


def _overlay() -> ModelHostDriftOrchestratorOverlay:
    data = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
    return ModelHostDriftOrchestratorOverlay.model_validate(data)


def _tick(now: dt.datetime, tick_id: uuid.UUID | None = None) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=tick_id or uuid.uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn24197-test",
        tick_interval_ms=1000,
    )


def _handler(sender: InMemoryHostDriftSender) -> HandlerHostDriftScheduled:
    return HandlerHostDriftScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=_overlay()),
        sender=sender,
        clock=lambda: T0 + dt.timedelta(seconds=5),
    )


def _payloads(
    output: Any,
) -> tuple[ModelHostDriftRunCompleted, ModelScheduleRunEvent]:
    run, schedule = output.events
    assert run.event_type == run_completed_topic()
    assert schedule.event_type == schedule_run_topic()
    assert isinstance(run.payload, ModelHostDriftRunCompleted)
    assert isinstance(schedule.payload, ModelScheduleRunEvent)
    return run.payload, schedule.payload


def test_golden_a_tick_sends_one_unit_to_each_host_and_reports_one_verdict_per_host() -> (
    None
):
    overlay = _overlay()
    sender = InMemoryHostDriftSender()

    run, schedule = _payloads(_handler(sender).handle(_tick(T0)))

    names = [row.name for row in overlay.hosts]
    assert sorted(u.target_host for u in sender.sent) == sorted(names)
    assert [v.host for v in run.verdicts] == names
    assert {v.verdict for v in run.verdicts} == {EnumHostDriftHostVerdict.IN_SYNC}
    assert run.outcome is EnumHostDriftRunOutcome.COMPLETED
    assert run.failed_hosts == ()
    assert schedule.outcome == "completed"
    assert schedule.job_id == overlay.job_id
    assert schedule.tick_id == run.tick_id


def test_each_unit_carries_its_hosts_name_and_paths_in_the_check_argv() -> None:
    overlay = _overlay()
    sender = InMemoryHostDriftSender()

    _handler(sender).handle(_tick(T0))

    for unit in sender.sent:
        row = next(r for r in overlay.hosts if r.name == unit.target_host)
        assert unit.argv[unit.argv.index("--host") + 1] == row.name
        assert unit.argv[unit.argv.index("--profiles") + 1] == row.profiles_path
        assert unit.timeout_seconds == overlay.result_timeout_seconds
        assert unit.repo == overlay.lab_work.repo
        assert unit.commit_sha == overlay.lab_work.commit_sha


def test_a_drifted_host_is_an_answer_and_the_run_still_completes() -> None:
    drifted = _overlay().hosts[1].name
    sender = InMemoryHostDriftSender(
        {
            drifted: (
                EnumLabWorkUnitStatus.COMPLETED,
                1,
                f"DRIFT {drifted} unit onex-x\nVERDICT {drifted}: DRIFTED findings=1",
            )
        }
    )

    run, schedule = _payloads(_handler(sender).handle(_tick(T0)))

    by_host = {v.host: v.verdict for v in run.verdicts}
    assert by_host[drifted] is EnumHostDriftHostVerdict.DRIFTED
    assert run.outcome is EnumHostDriftRunOutcome.COMPLETED
    assert schedule.outcome == "completed"


@pytest.mark.parametrize("silent", [False, True])
def test_silent_host_fails_the_run_naming_it_and_all_four_answering_completes(
    silent: bool,
) -> None:
    hosts = [row.name for row in _overlay().hosts]
    quiet = hosts[2]
    sender = InMemoryHostDriftSender(
        silent=frozenset({quiet}) if silent else frozenset()
    )

    run, schedule = _payloads(_handler(sender).handle(_tick(T0)))

    if not silent:
        assert run.outcome is EnumHostDriftRunOutcome.COMPLETED
        assert schedule.outcome == "completed"
        return
    assert run.outcome is EnumHostDriftRunOutcome.FAILED
    assert run.failed_hosts == (quiet,)
    assert schedule.outcome == "failed"
    assert quiet in schedule.detail
    by_host = {v.host: v for v in run.verdicts}
    assert by_host[quiet].verdict is EnumHostDriftHostVerdict.NO_RESULT
    others = [h for h in hosts if h != quiet]
    assert {by_host[h].verdict for h in others} == {EnumHostDriftHostVerdict.IN_SYNC}
    assert all(h not in schedule.detail for h in others)


def test_a_check_that_exits_four_fails_the_run_and_carries_the_output_tail() -> None:
    host = _overlay().hosts[0].name
    sender = InMemoryHostDriftSender(
        {
            host: (
                EnumLabWorkUnitStatus.COMPLETED,
                4,
                "item open past its escalation age",
            )
        }
    )

    run, schedule = _payloads(_handler(sender).handle(_tick(T0)))

    failed = next(v for v in run.verdicts if v.host == host)
    assert failed.verdict is EnumHostDriftHostVerdict.CHECK_FAILED
    assert failed.exit_code == 4
    assert "escalation age" in failed.output_tail
    assert run.outcome is EnumHostDriftRunOutcome.FAILED
    assert schedule.outcome == "failed"


def test_exit_one_with_no_verdict_line_is_a_failed_check_not_drift() -> None:
    host = _overlay().hosts[0].name
    sender = InMemoryHostDriftSender(
        {host: (EnumLabWorkUnitStatus.COMPLETED, 1, "Traceback: KeyError")}
    )

    run, _ = _payloads(_handler(sender).handle(_tick(T0)))

    verdict = next(v for v in run.verdicts if v.host == host)
    assert verdict.verdict is EnumHostDriftHostVerdict.CHECK_FAILED


def test_a_sender_that_raises_reads_as_no_result_for_that_host_only() -> None:
    hosts = [row.name for row in _overlay().hosts]

    class _Raising(InMemoryHostDriftSender):
        def send(self, request: Any) -> Any:
            if request.target_host == hosts[0]:
                raise ConnectionError("broker down")
            return super().send(request)

    run, _ = _payloads(_handler(_Raising()).handle(_tick(T0)))

    by_host = {v.host: v for v in run.verdicts}
    assert by_host[hosts[0]].verdict is EnumHostDriftHostVerdict.NO_RESULT
    assert "ConnectionError" in by_host[hosts[0]].detail
    assert by_host[hosts[1]].verdict is EnumHostDriftHostVerdict.IN_SYNC


def test_two_deliveries_of_one_tick_send_four_commands_not_eight() -> None:
    sender = InMemoryHostDriftSender()
    handler = _handler(sender)
    tick = _tick(T0)

    first = handler.handle(tick)
    second = handler.handle(tick)

    assert len(first.events) == 2
    assert second.events == ()
    assert len(sender.sent) == 4


def test_two_concurrent_deliveries_of_one_tick_send_one_set_of_four() -> None:
    sender = InMemoryHostDriftSender()
    handler = _handler(sender)
    tick = _tick(T0)
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def _deliver() -> None:
        barrier.wait()
        results.append(handler.handle(tick))

    threads = [threading.Thread(target=_deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(len(r.events) for r in results) == [0, 2]
    assert len(sender.sent) == 4


def test_a_tick_inside_the_interval_sends_nothing_and_the_next_due_tick_sends_again() -> (
    None
):
    overlay = _overlay()
    sender = InMemoryHostDriftSender()
    handler = _handler(sender)

    handler.handle(_tick(T0))
    inside = handler.handle(
        _tick(T0 + dt.timedelta(seconds=overlay.run_interval_seconds - 1))
    )
    due = handler.handle(_tick(T0 + dt.timedelta(seconds=overlay.run_interval_seconds)))

    assert inside.events == ()
    assert len(due.events) == 2
    assert len(sender.sent) == 8


def test_a_run_stopped_between_its_claim_and_its_send_ends_failed_and_the_next_tick_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def _build(overlay: ModelHostDriftOrchestratorOverlay) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("stopped after the claim")
        return build_sender(overlay)

    monkeypatch.setattr(scheduled_module, "build_sender", _build)
    handler = HandlerHostDriftScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=_overlay()),
        clock=lambda: T0 + dt.timedelta(seconds=5),
    )

    stopped_run, stopped_schedule = _payloads(handler.handle(_tick(T0)))
    assert stopped_run.outcome is EnumHostDriftRunOutcome.FAILED
    assert stopped_schedule.outcome == "failed"
    assert "stopped after the claim" in stopped_schedule.detail
    assert {v.verdict for v in stopped_run.verdicts} == {
        EnumHostDriftHostVerdict.NO_RESULT
    }

    retry = handler.handle(_tick(T0 + dt.timedelta(seconds=1)))
    retry_run, retry_schedule = _payloads(retry)
    assert retry_run.outcome is EnumHostDriftRunOutcome.COMPLETED
    assert retry_schedule.outcome == "completed"
    assert len(retry_run.verdicts) == 4
