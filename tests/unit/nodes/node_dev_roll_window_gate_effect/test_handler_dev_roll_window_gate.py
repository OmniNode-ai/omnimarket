# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler table of node_dev_roll_window_gate_effect: hold, tick (window, interval, health) and completed."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.events.runtime_deployment import (
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateRefusal,
    ModelDevRollGateDecision,
    ModelDevRollGateHeld,
    ModelDevRollGateRefused,
    ModelDevRollGateRollRecorded,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    FAILED_RELEASE,
    HEALTHY_FOR,
    MIN_INTERVAL,
    POLICY,
    WINDOW,
    Gate,
    command,
    completed,
)

pytestmark = pytest.mark.unit


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


def decisions(out: Sequence[BaseModel] | None) -> list[ModelDevRollGateDecision]:
    return [m for m in (out or []) if isinstance(m, ModelDevRollGateDecision)]


def released(out: Sequence[BaseModel] | None) -> list[ModelDeployPublishCommand]:
    return [m for m in (out or []) if isinstance(m, ModelDeployPublishCommand)]


def hold_three(gate: Gate) -> list[ModelDeployPublishCommand]:
    held = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2, 3)]
    for item in held:
        assert isinstance(gate.hold.handle(item), ModelDevRollGateHeld)
    return held


def test_hold_stores_a_dev_command_and_publishes_nothing_to_the_agent(
    gate: Gate,
) -> None:
    result = gate.hold.handle(command(number=1))
    assert isinstance(result, ModelDevRollGateHeld)
    assert result.held_count == 1
    assert result.repository == "omnimarket"
    assert len(gate.store.load().held) == 1


def test_hold_stores_a_repeated_correlation_id_once(gate: Gate) -> None:
    one = command(number=1)
    gate.hold.handle(one)
    again = gate.hold.handle(one)
    assert isinstance(again, ModelDevRollGateHeld)
    assert again.held_count == 1
    assert len(gate.store.load().held) == 1


def test_tick_inside_the_window_releases_nothing(gate: Gate) -> None:
    hold_three(gate)
    gate.clock.advance(
        30
    )  # the oldest is WINDOW-60 old at hold time, so 30 s on is still inside the window
    out = gate.tick.handle(a_tick(gate))
    assert released(out) == []
    (decision,) = decisions(out)
    assert decision.action == "hold"
    assert decision.hold_reason == "window_open"
    assert len(gate.store.load().held) == 3


def test_tick_after_the_window_releases_every_held_command_oldest_first(
    gate: Gate,
) -> None:
    held = hold_three(gate)
    gate.clock.advance(WINDOW)
    out = gate.tick.handle(a_tick(gate))
    sent = released(out)
    assert [c.correlation_id for c in sent] == [c.correlation_id for c in held]
    assert all(c.roll_window_released for c in sent)
    (decision,) = decisions(out)
    assert decision.action == "roll_now"
    assert decision.released_count == 3
    state = gate.store.load()
    assert state.held == ()
    assert state.last_roll is not None
    assert state.last_roll.finished_at is None
    assert set(state.released_ids) == {str(c.correlation_id) for c in held}


def test_a_released_command_is_the_commands_own_copy_with_only_the_flag_changed() -> (
    None
):
    gate = Gate()
    original = command(number=7, age_seconds=WINDOW)
    gate.hold.handle(original)
    out = gate.tick.handle(a_tick(gate))
    (sent,) = released(out)
    assert sent == original.model_copy(update={"roll_window_released": True})


def test_no_second_release_while_the_previous_roll_has_no_completion(
    gate: Gate,
) -> None:
    hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(WINDOW * 4)
    out = gate.tick.handle(a_tick(gate))
    assert released(out) == []
    (decision,) = decisions(out)
    assert decision.hold_reason == "roll_in_progress"


def test_after_a_healthy_finish_the_next_release_waits_for_the_interval_and_health(
    gate: Gate,
) -> None:
    held = hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    started = gate.clock.now
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(120)
    done = gate.completed.handle(completed(held[-1].correlation_id, at=gate.clock.now))
    assert isinstance(done, ModelDevRollGateRollRecorded)
    assert done.outcome == "finished_healthy"
    # window is long past for the new command, but the interval from the last start has not elapsed
    out = gate.tick.handle(a_tick(gate))
    assert released(out) == []
    (decision,) = decisions(out)
    assert decision.hold_reason == "min_start_interval"
    assert decision.hold_until == started + timedelta(seconds=MIN_INTERVAL)
    gate.clock.advance(MIN_INTERVAL)
    out = gate.tick.handle(a_tick(gate))
    assert len(released(out)) == 1
    assert (
        HEALTHY_FOR < MIN_INTERVAL
    )  # health bound is inside the interval in this policy


def test_an_unhealthy_finish_holds_until_the_failed_roll_release_bound(
    gate: Gate,
) -> None:
    held = hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(60)
    finished = gate.clock.now
    done = gate.completed.handle(
        completed(
            held[-1].correlation_id,
            at=finished,
            status=EnumRedeployStatus.FAILED,
        )
    )
    assert isinstance(done, ModelDevRollGateRollRecorded)
    assert done.outcome == "finished_unhealthy"
    gate.clock.advance(MIN_INTERVAL)
    out = gate.tick.handle(a_tick(gate))
    (decision,) = decisions(out)
    assert decision.hold_reason == "previous_roll_not_healthy"
    assert decision.hold_until == finished + timedelta(seconds=FAILED_RELEASE)
    gate.clock.now = finished + timedelta(seconds=FAILED_RELEASE)
    assert len(released(gate.tick.handle(a_tick(gate)))) == 1


def test_completed_ignores_a_command_the_gate_never_released(gate: Gate) -> None:
    hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    before = gate.store.load()
    assert gate.completed.handle(completed(uuid4(), at=gate.clock.now)) is None
    assert gate.store.load() == before


def test_tick_with_nothing_held_emits_nothing(gate: Gate) -> None:
    assert gate.tick.handle(a_tick(gate)) is None


def test_tick_with_the_gate_off_emits_nothing() -> None:
    gate = Gate(POLICY.model_copy(update={"mode": "off"}))
    gate.store.save(gate.store.load().model_copy(update={"held": ()}))
    assert gate.tick.handle(a_tick(gate)) is None


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (
            lambda: command(lane=EnumRuntimeLane.STABILITY_TEST),
            EnumDevRollGateRefusal.NOT_A_DEV_COMMAND,
        ),
        (
            lambda: command(requested_by="node_redeploy_orchestrator"),
            EnumDevRollGateRefusal.REQUESTER_NOT_A_CI_ISSUER,
        ),
        (
            lambda: command(requested_by="gha/omniclaude/pr-3"),
            EnumDevRollGateRefusal.REPOSITORY_UNKNOWN,
        ),
        (
            lambda: command(git_ref="origin/dev"),
            EnumDevRollGateRefusal.GIT_REF_NOT_A_SHA,
        ),
        (
            lambda: command(image_ref="registry.example/img:1"),
            EnumDevRollGateRefusal.PINNED_IMAGE,
        ),
    ],
)
def test_a_command_the_gate_cannot_hold_is_refused_and_passed_straight_on(
    gate: Gate, make: Any, reason: EnumDevRollGateRefusal
) -> None:
    sent = make()
    result = gate.hold.handle(sent)
    assert isinstance(result, list)
    refusal, passed = result
    assert isinstance(refusal, ModelDevRollGateRefused)
    assert refusal.reason is reason
    assert isinstance(passed, ModelDeployPublishCommand)
    assert passed.roll_window_released is True
    assert passed.correlation_id == sent.correlation_id
    assert gate.store.load().held == ()
