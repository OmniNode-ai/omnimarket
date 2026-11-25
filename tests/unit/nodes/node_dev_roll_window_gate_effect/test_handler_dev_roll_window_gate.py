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
from omnimarket.models.dev_roll import EnumDevRollHealth
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers import (
    HandlerDevRollWindowCompleted,
    HandlerDevRollWindowTick,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateAbandonReason,
    EnumDevRollGateRefusal,
    EnumDevRollGateRollOutcome,
    ModelDevRollGateAbandonedRoll,
    ModelDevRollGateDecision,
    ModelDevRollGateHeld,
    ModelDevRollGateRefused,
    ModelDevRollGateRollRecorded,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    DEADLINE,
    FAILED_RELEASE,
    HEALTHY_FOR,
    MIN_INTERVAL,
    NOW,
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
    gate.clock.advance(WINDOW)
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


def test_an_unhealthy_finish_holds_until_the_failed_roll_release_bound_from_the_roll_start(
    gate: Gate,
) -> None:
    held = hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    started = gate.clock.now
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
    # the failed-roll hold is counted from the roll's start, not from its finish
    assert decision.hold_until == started + timedelta(seconds=FAILED_RELEASE)
    assert finished < decision.hold_until
    gate.clock.now = started + timedelta(seconds=FAILED_RELEASE)
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


def test_the_completion_of_any_released_command_ends_the_roll_and_the_newest_one_completing_later_sets_its_health(
    gate: Gate,
) -> None:
    held = hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    gate.clock.advance(60)
    # an older command of the batch completing ends the roll: the agent publishes a completion only for a command it
    # ran, so the newest may be one it skipped and never complete
    first_at = gate.clock.now
    ended = gate.completed.handle(completed(held[0].correlation_id, at=first_at))
    assert isinstance(ended, ModelDevRollGateRollRecorded)
    last = gate.store.load().last_roll
    assert last is not None
    assert last.finished_at == first_at
    assert last.job_id == str(held[-1].correlation_id)
    # an unfolded batch runs in order: the newest completing later is the final word on the roll's health
    gate.clock.advance(60)
    done = gate.completed.handle(
        completed(
            held[-1].correlation_id,
            at=gate.clock.now,
            status=EnumRedeployStatus.FAILED,
        )
    )
    assert isinstance(done, ModelDevRollGateRollRecorded)
    last = gate.store.load().last_roll
    assert last is not None
    assert last.finished_at == gate.clock.now
    assert last.health is EnumDevRollHealth.UNHEALTHY


def test_a_roll_with_no_completion_by_the_deadline_is_abandoned_and_the_held_commands_go_to_the_next_roll(
    gate: Gate,
) -> None:
    hold_three(gate)
    gate.clock.advance(WINDOW)
    first = released(gate.tick.handle(a_tick(gate)))
    started = gate.clock.now
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(DEADLINE - 1)
    out = gate.tick.handle(a_tick(gate))
    assert [d.hold_reason for d in decisions(out)] == ["roll_in_progress"]
    assert gate.store.load().abandoned == ()
    gate.clock.advance(1)  # the deadline
    out = gate.tick.handle(a_tick(gate))
    recorded = [m for m in (out or []) if isinstance(m, ModelDevRollGateRollRecorded)]
    assert [r.outcome for r in recorded] == [EnumDevRollGateRollOutcome.ABANDONED]
    assert isinstance(recorded[0].outcome, EnumDevRollGateRollOutcome)
    # released at the deadline, not FAILED_RELEASE seconds after it
    assert len(released(out)) == 1
    (decision,) = decisions(out)
    assert decision.action == "roll_now"
    state = gate.store.load()
    assert state.held == ()
    (abandoned,) = state.abandoned
    assert isinstance(abandoned, ModelDevRollGateAbandonedRoll)
    assert abandoned.command_correlation_id == str(first[-1].correlation_id)
    assert abandoned.reason is EnumDevRollGateAbandonReason.COMPLETION_DEADLINE_PASSED
    assert abandoned.runtime_started_at == started
    assert abandoned.abandoned_at == started + timedelta(seconds=DEADLINE)
    assert state.last_roll is not None
    assert state.last_roll.job_id != abandoned.command_correlation_id


def test_a_late_completion_of_an_abandoned_roll_changes_nothing(gate: Gate) -> None:
    held = hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    gate.clock.advance(DEADLINE)
    gate.tick.handle(a_tick(gate))
    before = gate.store.load()
    assert before.last_roll is None
    gate.clock.advance(60)
    assert (
        gate.completed.handle(completed(held[-1].correlation_id, at=gate.clock.now))
        is None
    )
    assert gate.store.load() == before


def test_a_roll_the_earlier_deadline_code_ended_with_health_unknown_is_abandoned_too(
    gate: Gate,
) -> None:
    """The state a gate that ran the earlier code holds: finished at the deadline, health unknown, five commands held."""
    hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    state = gate.store.load()
    last = state.last_roll
    assert last is not None
    assert last.runtime_started_at is not None
    finished = last.runtime_started_at + timedelta(seconds=DEADLINE)
    gate.store.save(
        state.model_copy(
            update={"last_roll": last.model_copy(update={"finished_at": finished})}
        )
    )
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(DEADLINE + 60)
    out = gate.tick.handle(a_tick(gate))
    assert len(released(out)) == 1
    (abandoned,) = gate.store.load().abandoned
    assert abandoned.abandoned_at == finished
    assert abandoned.command_correlation_id == last.job_id


def test_an_unhealthy_finished_roll_still_waits_the_failed_roll_bound_from_its_start(
    gate: Gate,
) -> None:
    held = hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    started = gate.clock.now
    gate.clock.advance(60)
    gate.completed.handle(
        completed(
            held[-1].correlation_id,
            at=gate.clock.now,
            status=EnumRedeployStatus.FAILED,
        )
    )
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(DEADLINE)
    out = gate.tick.handle(a_tick(gate))
    assert released(out) == []
    assert gate.store.load().abandoned == ()
    (decision,) = decisions(out)
    assert decision.hold_reason == "previous_roll_not_healthy"
    assert decision.hold_until == started + timedelta(seconds=FAILED_RELEASE)


def test_failing_control_without_the_deadline_the_gate_would_hold_for_ever(
    gate: Gate,
) -> None:
    """The recovery above is real: just before the deadline the same state is still roll_in_progress."""
    hold_three(gate)
    gate.clock.advance(WINDOW)
    gate.tick.handle(a_tick(gate))
    gate.hold.handle(command(number=9, age_seconds=WINDOW))
    gate.clock.advance(DEADLINE - 1)
    out = gate.tick.handle(a_tick(gate))
    assert released(out) == []
    with pytest.raises(AssertionError):
        assert [d.hold_reason for d in decisions(out)] == ["previous_roll_not_healthy"]


def test_an_overdue_roll_is_recorded_even_when_nothing_is_held(gate: Gate) -> None:
    gate.hold.handle(command(number=1, age_seconds=WINDOW))
    gate.tick.handle(a_tick(gate))
    gate.clock.advance(DEADLINE)
    out = gate.tick.handle(a_tick(gate))
    assert [type(m).__name__ for m in out or []] == ["ModelDevRollGateRollRecorded"]
    assert len(gate.store.load().abandoned) == 1
    assert gate.tick.handle(a_tick(gate)) is None


def test_a_naive_requested_at_is_replaced_by_the_time_the_gate_saw_the_command(
    gate: Gate,
) -> None:
    item = command(number=1).model_copy(
        update={"requested_at": NOW.replace(tzinfo=None)}
    )
    assert isinstance(gate.hold.handle(item), ModelDevRollGateHeld)
    (held,) = gate.store.load().held
    assert held.merged_at == gate.clock.now
    gate.clock.advance(WINDOW)
    assert (
        len(released(gate.tick.handle(a_tick(gate)))) == 1
    )  # the tick no longer raises


def test_a_future_requested_at_is_clamped_to_the_time_the_gate_saw_the_command(
    gate: Gate,
) -> None:
    item = command(number=1).model_copy(
        update={"requested_at": NOW + timedelta(hours=1)}
    )
    gate.hold.handle(item)
    (held,) = gate.store.load().held
    assert held.merged_at == gate.clock.now
    gate.clock.advance(WINDOW)
    assert len(released(gate.tick.handle(a_tick(gate)))) == 1


def test_switching_the_gate_off_releases_what_it_holds_oldest_first(gate: Gate) -> None:
    held = hold_three(gate)
    off = Gate(POLICY.model_copy(update={"mode": "off"}), gate.clock)
    off.store.save(gate.store.load())
    out = off.tick.handle(a_tick(off))
    assert [c.correlation_id for c in released(out)] == [c.correlation_id for c in held]
    assert all(c.roll_window_released for c in released(out))
    assert off.store.load().held == ()
    assert off.tick.handle(a_tick(off)) is None


def test_failing_control_a_gate_still_live_releases_nothing_before_the_window(
    gate: Gate,
) -> None:
    """The flush above is real: the same state under a live policy releases nothing inside the window."""
    hold_three(gate)
    assert released(gate.tick.handle(a_tick(gate))) == []


def test_with_no_gate_overlay_installed_a_tick_and_a_completion_do_nothing(
    tmp_path: Any,
) -> None:
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    tick = HandlerDevRollWindowTick(environ=environ)
    done = HandlerDevRollWindowCompleted(environ=environ)
    clock_gate = Gate()
    assert tick.handle(a_tick(clock_gate)) is None
    assert done.handle(completed(uuid4(), at=NOW)) is None
