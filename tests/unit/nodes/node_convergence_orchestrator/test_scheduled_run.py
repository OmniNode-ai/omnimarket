# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime-tick route of the convergence orchestrator (OMN-24198): golden, gate, error and failing-control cases.

The handler is built the way the runtime builds it, with the real schedule gate over the in-memory profile overlay and an
in-memory lab-work sender; only the sender's answers (what each host's command printed) are scripted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceCloneStatus,
    EnumConvergenceHostOutcome,
    EnumConvergenceMode,
    EnumConvergenceRunOutcome,
    ModelConvergenceOrchestratorOverlay,
    ModelConvergenceRunCompleted,
)
from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.handler_convergence_scheduled import (
    NODE_NAME,
    HandlerConvergenceScheduled,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.sender_in_memory import (
    InMemoryConvergenceSender,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
T0 = dt.datetime(2026, 11, 14, 4, 0, tzinfo=dt.UTC)
HOSTS = ("example-host-a", "example-host-b", "example-host-c")
Scripted = dict[str, tuple[EnumLabWorkUnitStatus, int | None, str]]


def overlay(mode: str = "observe") -> ModelConvergenceOrchestratorOverlay:
    data: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "overlays" / "in_memory.yaml").read_text()
    )
    data["mode"] = mode
    return ModelConvergenceOrchestratorOverlay.model_validate(data)


def tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn24198-test",
        tick_interval_ms=1000,
    )


def handler(
    sender: InMemoryConvergenceSender, mode: str = "observe"
) -> HandlerConvergenceScheduled:
    return HandlerConvergenceScheduled(
        gate=ScheduleGate(NODE_NAME, declaration=overlay(mode)),
        sender=sender,
        clock=lambda: T0 + dt.timedelta(seconds=5),
    )


def payloads(output: Any) -> tuple[ModelConvergenceRunCompleted, ModelScheduleRunEvent]:
    run, schedule = (envelope.payload for envelope in output.events)
    assert isinstance(run, ModelConvergenceRunCompleted)
    assert isinstance(schedule, ModelScheduleRunEvent)
    return run, schedule


def done(exit_code: int, text: str) -> tuple[EnumLabWorkUnitStatus, int, str]:
    return EnumLabWorkUnitStatus.COMPLETED, exit_code, text


def test_a_due_tick_sends_one_command_per_host_and_ends_with_one_result_per_host() -> (
    None
):
    sender = InMemoryConvergenceSender(
        {
            h: done(0, f"CLEAN         /r/{h}/platform: on main at the upstream tip")
            for h in HOSTS
        }
    )

    run, schedule = payloads(handler(sender).handle(tick(T0)))

    assert sorted(r.target_host for r in sender.sent) == sorted(HOSTS)
    assert len(sender.sent) == len(HOSTS) == len(run.hosts)
    assert {h.host for h in run.hosts} == set(HOSTS)
    assert {h.outcome for h in run.hosts} == {EnumConvergenceHostOutcome.CLEAN}
    assert run.outcome is EnumConvergenceRunOutcome.COMPLETED
    assert schedule.outcome == "completed"
    assert schedule.job_id == overlay().job_id


def test_a_clean_and_a_dirty_clone_each_return_their_own_typed_result() -> None:
    sender = InMemoryConvergenceSender(
        {
            "example-host-a": done(
                0, "CLEAN         /r/platform: on main at the upstream tip"
            ),
            "example-host-b": done(
                0,
                "WOULD_REFUSE   /r/platform: holds local work: modified 1 (a.py); an act run refuses it",
            ),
            "example-host-c": done(
                0, "WOULD_MOVE     /r/platform: would fast-forward 3 commit(s) on main"
            ),
        }
    )

    run, _ = payloads(handler(sender).handle(tick(T0)))

    by_host = {h.host: h for h in run.hosts}
    assert by_host["example-host-a"].outcome is EnumConvergenceHostOutcome.CLEAN
    assert by_host["example-host-b"].outcome is EnumConvergenceHostOutcome.REFUSED
    assert (
        by_host["example-host-c"].outcome is EnumConvergenceHostOutcome.OBSERVED_PENDING
    )
    assert (
        by_host["example-host-a"].clones[0].status is EnumConvergenceCloneStatus.CLEAN
    )
    assert (
        by_host["example-host-b"].clones[0].status
        is EnumConvergenceCloneStatus.WOULD_REFUSE
    )


def test_a_clone_the_host_cannot_converge_is_a_typed_refusal_and_the_run_ends_failed() -> (
    None
):
    sender = InMemoryConvergenceSender(
        {
            "example-host-b": done(
                0,
                "REFUSED       /r/omnidash refused 400s: holds local work: modified 2 (x.ts, y.ts) [alarm: delivered]",
            )
        }
    )

    run, schedule = payloads(handler(sender, "act").handle(tick(T0)))

    refused = next(h for h in run.hosts if h.host == "example-host-b")
    assert refused.outcome is EnumConvergenceHostOutcome.REFUSED
    (refusal,) = refused.refusals
    assert refusal.clone == "/r/omnidash"
    assert "modified 2 (x.ts, y.ts)" in refusal.reason
    assert run.outcome is EnumConvergenceRunOutcome.FAILED
    assert schedule.outcome == "failed"


def test_a_nonzero_exit_with_no_clone_named_fails_the_host_and_the_run() -> None:
    sender = InMemoryConvergenceSender({"example-host-a": done(3, "Traceback: boom")})

    run, schedule = payloads(handler(sender, "act").handle(tick(T0)))

    failed = next(h for h in run.hosts if h.host == "example-host-a")
    assert failed.outcome is EnumConvergenceHostOutcome.FAILED
    assert failed.exit_code == 3
    assert run.outcome is EnumConvergenceRunOutcome.FAILED
    assert schedule.outcome == "failed"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (EnumLabWorkUnitStatus.TIMED_OUT, EnumConvergenceHostOutcome.TIMED_OUT),
        (
            EnumLabWorkUnitStatus.INFRA_ERROR,
            EnumConvergenceHostOutcome.TRANSPORT_UNAVAILABLE,
        ),
        (EnumLabWorkUnitStatus.REFUSED, EnumConvergenceHostOutcome.FAILED),
    ],
)
def test_a_unit_that_did_not_run_to_an_exit_is_never_a_completed_host(
    status: EnumLabWorkUnitStatus, expected: EnumConvergenceHostOutcome
) -> None:
    sender = InMemoryConvergenceSender({"example-host-c": (status, None, "")})

    run, _ = payloads(handler(sender).handle(tick(T0)))

    assert next(h for h in run.hosts if h.host == "example-host-c").outcome is expected
    assert run.outcome is EnumConvergenceRunOutcome.FAILED


def test_observe_sends_the_observe_command_and_act_sends_the_act_command() -> None:
    observe_sender = InMemoryConvergenceSender()
    act_sender = InMemoryConvergenceSender()

    handler(observe_sender, "observe").handle(tick(T0))
    handler(act_sender, "act").handle(tick(T0))

    assert all("--observe" in r.argv for r in observe_sender.sent)
    assert all("--observe" not in r.argv for r in act_sender.sent)
    assert {r.argv[1] for r in observe_sender.sent} == {
        f"OMNI_HOME={overlay().hosts[0].registry_root}"
    }
    assert overlay("observe").mode is EnumConvergenceMode.OBSERVE


def test_a_tick_inside_the_interval_sends_nothing() -> None:
    sender = InMemoryConvergenceSender()
    h = handler(sender)

    first = h.handle(tick(T0))
    second = h.handle(tick(T0 + dt.timedelta(seconds=10)))
    third = h.handle(tick(T0 + dt.timedelta(seconds=overlay().run_interval_seconds)))

    assert len(first.events) == 2
    assert second.events == ()
    assert len(third.events) == 2
    assert len(sender.sent) == 2 * len(HOSTS)


def test_a_missing_overlay_is_refused_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    sender = InMemoryConvergenceSender()

    output = HandlerConvergenceScheduled(sender=sender).handle(tick(T0))

    assert output.events == ()
    assert sender.sent == []
