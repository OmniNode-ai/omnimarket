# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The roll gate types an abandoned roll, bounds the failed-roll hold from the roll's start, and alarms on a held command's age.

Four falsifier groups, named for the ticket's ``-k`` expressions: ``roll_gate_abandoned_typed``,
``roll_gate_release_from_start``, ``roll_gate_held_age_alarm`` and ``roll_gate_incident_replay``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.events.runtime_deployment import (
    EnumRedeployStatus,
    ModelDeployPublishCommand,
)
from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers import (
    HandlerDevRollWindowTick,
    InMemoryDevRollGateStore,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateAbandonReason,
    ModelDevRollGateAbandonedRoll,
    ModelDevRollGateDecision,
    ModelDevRollGateState,
    ModelDevRollWindowGatePolicy,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    DEADLINE,
    FAILED_RELEASE,
    HELD_AGE_ALARM,
    POLICY,
    WINDOW,
    Clock,
    Gate,
    command,
    completed,
)

pytestmark = pytest.mark.unit

ALARM_CONDITION = "dev_roll_held_command_age"
ALARM_AFTER = HELD_AGE_ALARM
FIXTURE = Path(__file__).parent / "fixtures" / "gate_state_2026_11_22T2141Z.json"


def policy_with(**update: Any) -> ModelDevRollWindowGatePolicy:
    """The shared policy with some values replaced."""
    return POLICY.model_copy(update=update)


def a_tick(gate: Gate) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=gate.clock.now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=gate.clock.now,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


def released(out: Sequence[BaseModel] | None) -> list[ModelDeployPublishCommand]:
    return [m for m in out or [] if isinstance(m, ModelDeployPublishCommand)]


def decisions(out: Sequence[BaseModel] | None) -> list[ModelDevRollGateDecision]:
    return [m for m in out or [] if isinstance(m, ModelDevRollGateDecision)]


def alarms(out: Sequence[BaseModel] | None) -> list[ModelLabAlarmDeliveryRequest]:
    return [m for m in out or [] if isinstance(m, ModelLabAlarmDeliveryRequest)]


def first_roll(gate: Gate) -> tuple[str, datetime]:
    """Hold three commands, let the window pass, release them: the roll's job id and start."""
    for number in (1, 2, 3):
        gate.hold.handle(command(number=number, age_seconds=WINDOW))
    gate.clock.advance(WINDOW)
    out = gate.tick.handle(a_tick(gate))
    sent = released(out)
    assert len(sent) == 3
    return str(sent[-1].correlation_id), gate.clock.now


# --- AC1: the abandonment is typed state ---------------------------------------------------------


def test_roll_gate_abandoned_typed_state_carries_the_enum_reason_and_both_times() -> (
    None
):
    """6610 already writes this state, so this test passes on the base commit too."""
    gate = Gate(policy_with())
    job_id, started = first_roll(gate)
    gate.clock.advance(DEADLINE)
    gate.tick.handle(a_tick(gate))
    state = gate.store.load()
    (abandoned,) = state.abandoned
    assert abandoned.command_correlation_id == job_id
    assert abandoned.reason is EnumDevRollGateAbandonReason.COMPLETION_DEADLINE_PASSED
    assert abandoned.runtime_started_at == started
    assert abandoned.abandoned_at == started + timedelta(seconds=DEADLINE)
    # the file the store writes holds the typed reason, not a bare outcome string
    again = ModelDevRollGateState.model_validate_json(state.model_dump_json())
    assert (
        again.abandoned[0].reason
        is EnumDevRollGateAbandonReason.COMPLETION_DEADLINE_PASSED
    )
    assert json.loads(state.model_dump_json())["abandoned"][0]["reason"] == (
        "completion_deadline_passed"
    )


def test_roll_gate_abandoned_typed_reason_is_an_enum_never_a_bare_string() -> None:
    field = ModelDevRollGateAbandonedRoll.model_fields["reason"]
    assert field.annotation is EnumDevRollGateAbandonReason
    with pytest.raises(ValueError, match="reason"):
        ModelDevRollGateAbandonedRoll(
            command_correlation_id="job",
            git_ref="ref",
            reason="completion_overdue",  # type: ignore[arg-type]
            runtime_started_at=datetime(2026, 11, 22, tzinfo=UTC),
            abandoned_at=datetime(2026, 11, 22, tzinfo=UTC),
        )


# --- AC2: the failed-roll hold is counted from the roll's start ----------------------------------


def test_roll_gate_release_from_start_after_an_unhealthy_finish() -> None:
    gate = Gate(policy_with())
    job_id, started = first_roll(gate)
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(600)
    gate.completed.handle(
        completed(job_id, at=gate.clock.now, status=EnumRedeployStatus.FAILED)
    )
    assert gate.store.load().last_roll is not None
    release_at = started + timedelta(seconds=FAILED_RELEASE)
    gate.clock.now = release_at - timedelta(seconds=1)
    out = gate.tick.handle(a_tick(gate))
    assert released(out) == []
    (decision,) = decisions(out)
    assert decision.hold_reason == "previous_roll_not_healthy"
    assert decision.hold_until == release_at
    gate.clock.now = release_at
    assert len(released(gate.tick.handle(a_tick(gate)))) == 1


@pytest.mark.parametrize("deadline", [FAILED_RELEASE + 1800, FAILED_RELEASE * 2])
def test_roll_gate_release_from_start_even_when_the_deadline_is_longer(
    deadline: int,
) -> None:
    """A completion deadline past the release bound still frees the commands by the bound."""
    gate = Gate(policy_with(roll_completion_deadline_seconds=deadline))
    _, started = first_roll(gate)
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    release_at = started + timedelta(seconds=FAILED_RELEASE)
    gate.clock.now = release_at - timedelta(seconds=1)
    assert released(gate.tick.handle(a_tick(gate))) == []
    gate.clock.now = release_at
    out = gate.tick.handle(a_tick(gate))
    assert len(released(out)) == 1
    (abandoned,) = gate.store.load().abandoned
    assert abandoned.runtime_started_at == started
    assert abandoned.abandoned_at == release_at


# --- AC3: a held command older than the overlay's bound alarms once ------------------------------


def test_roll_gate_held_age_alarm_names_the_roll_and_the_held_commands_once() -> None:
    gate = Gate(policy_with(roll_completion_deadline_seconds=3 * 3600))
    job_id, _ = first_roll(gate)
    old = command(number=8, requested_by="gha/omnimarket/pr-6586")
    gate.hold.handle(old)
    gate.clock.advance(ALARM_AFTER)  # exactly as old as the bound: not older
    assert alarms(gate.tick.handle(a_tick(gate))) == []
    gate.clock.advance(1)
    (request,) = alarms(gate.tick.handle(a_tick(gate)))
    assert request.condition == ALARM_CONDITION
    assert request.outcome is EnumDeliveryCondition.ALARM
    (alarm,) = request.alarms
    for text in (alarm.subject, alarm.detail):
        assert job_id in text
        assert str(old.correlation_id) in text
    assert "gha/omnimarket/pr-6586" in alarm.detail
    # one alarm per episode: the next ticks, with the same command still held, raise nothing
    for _ in range(3):
        gate.clock.advance(60)
        assert alarms(gate.tick.handle(a_tick(gate))) == []


def test_roll_gate_held_age_alarm_is_silent_while_every_held_command_is_younger() -> (
    None
):
    gate = Gate(policy_with(roll_completion_deadline_seconds=3 * 3600))
    first_roll(gate)
    gate.hold.handle(command(number=8))
    for _ in range(ALARM_AFTER // 300):
        gate.clock.advance(300)
        assert alarms(gate.tick.handle(a_tick(gate))) == []
    assert gate.store.load().held != ()


def test_roll_gate_held_age_alarm_names_a_later_command_when_it_too_grows_old() -> None:
    gate = Gate(policy_with(roll_completion_deadline_seconds=3 * 3600))
    job_id, _ = first_roll(gate)
    first = command(number=8)
    gate.hold.handle(first)
    gate.clock.advance(1200)
    second = command(number=9)
    gate.hold.handle(second)
    gate.clock.advance(ALARM_AFTER - 1200 + 1)
    (one,) = alarms(gate.tick.handle(a_tick(gate)))
    assert str(first.correlation_id) in one.alarms[0].detail
    assert str(second.correlation_id) not in one.alarms[0].detail
    gate.clock.advance(1200)
    (two,) = alarms(gate.tick.handle(a_tick(gate)))
    for text in (two.alarms[0].subject, two.alarms[0].detail):
        assert job_id in text
        assert str(second.correlation_id) in text
    assert two.alarms[0].subject != one.alarms[0].subject
    assert two.correlation_id != one.correlation_id


def test_roll_gate_held_age_alarm_bound_is_an_overlay_value() -> None:
    """A different declared bound moves the alarm: the handler carries no literal of its own."""
    gate = Gate(
        policy_with(
            held_age_alarm_after_seconds=300, roll_completion_deadline_seconds=3 * 3600
        )
    )
    first_roll(gate)
    gate.hold.handle(command(number=8))
    gate.clock.advance(300)
    assert alarms(gate.tick.handle(a_tick(gate))) == []
    gate.clock.advance(1)
    assert len(alarms(gate.tick.handle(a_tick(gate)))) == 1


def test_roll_gate_held_age_alarm_state_survives_a_new_handler_over_the_same_store() -> (
    None
):
    """The once-only record is in the gate's durable state, so a runtime restart does not repeat the alarm."""
    policy = policy_with(roll_completion_deadline_seconds=3 * 3600)
    gate = Gate(policy)
    first_roll(gate)
    gate.hold.handle(command(number=8))
    gate.clock.advance(ALARM_AFTER + 1)
    assert len(alarms(gate.tick.handle(a_tick(gate)))) == 1
    restarted = HandlerDevRollWindowTick(gate.store, policy=policy, clock=gate.clock)
    assert alarms(restarted.handle(a_tick(gate))) == []


# --- AC4: replay of the gate state of 2026-11-22T21:41Z ------------------------------------------


def test_roll_gate_incident_replay_releases_at_once_and_alarms_naming_roll_and_commands() -> (
    None
):
    doc = json.loads(FIXTURE.read_text())
    assert "fixture_provenance" in doc
    state = ModelDevRollGateState.model_validate(doc["state"])
    assert state.last_roll is not None
    assert state.last_roll.job_id == "9088a7e7"
    assert [h.command.requested_by for h in state.held] == [
        f"gha/omnimarket/pr-{n}" for n in (6570, 6581, 6586, 6603, 6607)
    ]
    now = datetime(2026, 11, 22, 21, 41, 0, tzinfo=UTC)
    store = InMemoryDevRollGateStore()
    store.save(state)
    policy = policy_with(roll_completion_deadline_seconds=3600)
    tick = HandlerDevRollWindowTick(store, policy=policy, clock=Clock(now))
    out = tick.handle(
        ModelRuntimeTick(
            now=now,
            tick_id=uuid4(),
            sequence_number=1,
            scheduled_at=now,
            correlation_id=uuid4(),
            scheduler_id="replay",
            tick_interval_ms=1000,
        )
    )
    # the hold ends at once, not at finished_at + 3600 (22:24:50Z)
    assert len(released(out)) == 5
    assert [r.requested_by for r in released(out)] == [
        h.command.requested_by for h in state.held
    ]
    # and the five commands, all older than the bound, raise one alarm naming the roll and each command
    (request,) = alarms(out)
    (alarm,) = request.alarms
    assert "9088a7e7" in alarm.subject
    for held in state.held:
        assert str(held.command.correlation_id) in alarm.detail
        assert held.command.requested_by in alarm.detail
    (abandoned,) = store.load().abandoned
    assert abandoned.command_correlation_id == "9088a7e7"
    assert abandoned.reason is EnumDevRollGateAbandonReason.COMPLETION_DEADLINE_PASSED
    # the hold from the roll's start: 20:24:50Z + 3600 s is 21:24:50Z, already past at 21:41Z
    assert state.last_roll.runtime_started_at is not None
    assert state.last_roll.runtime_started_at + timedelta(seconds=FAILED_RELEASE) < now
