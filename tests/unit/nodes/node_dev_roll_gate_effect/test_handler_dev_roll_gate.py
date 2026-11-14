# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler table for node_dev_roll_gate_effect: hold, tick, decided, completed, rejected and the durable store."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnimarket.events.runtime_deployment import (
    EnumDeployRejectionReason,
    EnumRedeployStatus,
    ModelDeployRebuildCompleted,
    ModelDeployRebuildRejected,
)
from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    EnumDevRollEventRefusal,
    EnumDevRollHealth,
    EnumDevRollHoldReason,
    ModelDevRollDecided,
    ModelDevRollEventRefused,
    ModelDevRollFacts,
    ModelDevRollHoldRequest,
    ModelDevRollReleased,
    ModelDevRollRequest,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers import (
    FileDevRollGateStore,
    HandlerDevRollGateCompleted,
    HandlerDevRollGateDecided,
    HandlerDevRollGateHold,
    HandlerDevRollGateRejected,
    HandlerDevRollGateTick,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
    ModelDevRollGateTick,
)
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import (
    NOW,
    ROLL_BOUND_SECONDS,
    command,
    gate_policy,
)

pytestmark = pytest.mark.unit


def _hold(store, clock, held, policy=None):
    handler = HandlerDevRollGateHold(store, policy=policy or gate_policy(), clock=clock)
    return handler.handle(ModelDevRollHoldRequest(command=held))


def _decided(
    correlation_id: UUID,
    rolled: tuple[UUID, ...],
    *,
    action: EnumDevRollAction = EnumDevRollAction.ROLL_NOW,
    refused: tuple[UUID, ...] = (),
) -> ModelDevRollDecided:
    roll = action is EnumDevRollAction.ROLL_NOW
    return ModelDevRollDecided(
        correlation_id=correlation_id,
        observed_at=NOW,
        action=action,
        hold_reason=None if roll else EnumDevRollHoldReason.WINDOW_OPEN,
        hold_until=None,
        shadow=True,
        would_request=ModelDevRollRequest(
            correlation_id=correlation_id,
            requested_by="test",
            scope="full",
            runtime_lane="dev",
            git_ref="c" * 40,
            requested_at=NOW,
        )
        if roll
        else None,
        rolled_event_ids=rolled if roll else (),
        newest_sha_by_repository={},
        oldest_pending_merged_at=None,
        longest_wait_seconds=0,
        refused_events=tuple(
            ModelDevRollEventRefused(
                event_id=e,
                repository="x",
                reason=EnumDevRollEventRefusal.UNKNOWN_REPOSITORY,
            )
            for e in refused
        ),
    )


def _decide(store, clock, decided):
    return HandlerDevRollGateDecided(store, policy=gate_policy(), clock=clock).handle(
        decided
    )


def test_hold_stores_the_command_and_asks_for_a_decision(store, clock) -> None:
    held = command()
    facts = _hold(store, clock, held)
    assert isinstance(facts, ModelDevRollFacts)
    assert [h.command for h in store.load().held] == [held]
    assert str(facts.correlation_id) in store.load().requested_correlation_ids


def test_hold_of_a_repeated_correlation_id_is_not_held_twice(store, clock) -> None:
    held = command()
    _hold(store, clock, held)
    again = _hold(store, clock, held)
    assert isinstance(again, ModelDevRollGateObserved)
    assert again.reason is EnumDevRollGateObservedReason.ALREADY_HELD
    assert len(store.load().held) == 1


def test_hold_in_mode_off_releases_the_command_at_once_and_holds_nothing(
    store, clock
) -> None:
    held = command()
    out = _hold(store, clock, held, gate_policy(mode="off"))
    assert isinstance(out, ModelDevRollReleased)
    assert out.commands == (held,)
    assert store.load().held == ()


def test_a_command_that_names_neither_repository_nor_sha_is_still_held(
    store, clock
) -> None:
    odd = command().model_copy(update={"requested_by": "someone", "git_ref": None})
    facts = _hold(store, clock, odd)
    assert isinstance(facts, ModelDevRollFacts)
    (event,) = facts.pending_events
    assert event.repository == "omnimarket"
    assert len(event.sha) == 40


def test_decided_roll_now_for_its_own_request_releases_only_what_it_rolled_oldest_first(
    store, clock
) -> None:
    older, newer, late = command(), command(), command()
    _hold(store, clock, newer)
    clock.advance(-5)  # held_at orders the release, not arrival order
    _hold(store, clock, older)
    clock.advance(10)
    facts = _hold(store, clock, late)
    assert isinstance(facts, ModelDevRollFacts)
    # the decision rolled the first two only: the third was held after its facts
    request_id = UUID(store.load().requested_correlation_ids[1])
    out = _decide(
        store,
        clock,
        _decided(request_id, (older.correlation_id, newer.correlation_id)),
    )
    assert isinstance(out, ModelDevRollReleased)
    assert [c.correlation_id for c in out.commands] == [
        older.correlation_id,
        newer.correlation_id,
    ]
    assert [h.command.correlation_id for h in store.load().held] == [
        late.correlation_id
    ]
    state = store.load()
    assert state.outstanding == (
        str(older.correlation_id),
        str(newer.correlation_id),
    )
    assert state.last_roll is not None
    assert state.last_roll.finished_at is None
    assert state.last_roll.git_ref == "c" * 40


def test_a_decision_the_gate_never_asked_for_releases_nothing(store, clock) -> None:
    held = command()
    _hold(store, clock, held)
    out = _decide(store, clock, _decided(uuid4(), (held.correlation_id,)))
    assert isinstance(out, ModelDevRollGateObserved)
    assert out.reason is EnumDevRollGateObservedReason.NOT_OURS
    assert len(store.load().held) == 1


def test_a_hold_decision_releases_nothing(store, clock) -> None:
    held = command()
    _hold(store, clock, held)
    request_id = UUID(store.load().requested_correlation_ids[0])
    out = _decide(store, clock, _decided(request_id, (), action=EnumDevRollAction.HOLD))
    assert isinstance(out, ModelDevRollGateObserved)
    assert out.reason is EnumDevRollGateObservedReason.HELD_BY_DECISION
    assert out.detail == "window_open"


def test_a_second_roll_now_while_the_first_roll_is_outstanding_releases_nothing(
    store, clock
) -> None:
    one, two = command(), command()
    _hold(store, clock, one)
    first_request = UUID(store.load().requested_correlation_ids[0])
    _hold(store, clock, two)
    second_request = UUID(store.load().requested_correlation_ids[1])
    assert isinstance(
        _decide(store, clock, _decided(first_request, (one.correlation_id,))),
        ModelDevRollReleased,
    )
    out = _decide(store, clock, _decided(second_request, (two.correlation_id,)))
    assert isinstance(out, ModelDevRollGateObserved)
    assert out.reason is EnumDevRollGateObservedReason.ROLL_IN_PROGRESS
    assert [h.command.correlation_id for h in store.load().held] == [two.correlation_id]


def test_a_repeated_roll_now_releases_nothing_the_second_time(store, clock) -> None:
    held = command()
    _hold(store, clock, held)
    request_id = UUID(store.load().requested_correlation_ids[0])
    decided = _decided(request_id, (held.correlation_id,))
    assert isinstance(_decide(store, clock, decided), ModelDevRollReleased)
    again = _decide(store, clock, decided)
    assert isinstance(again, ModelDevRollGateObserved)
    assert again.reason in (
        EnumDevRollGateObservedReason.ROLL_IN_PROGRESS,
        EnumDevRollGateObservedReason.NOTHING_HELD,
    )


def test_an_event_the_batching_node_refused_rides_the_release_not_lost(
    store, clock
) -> None:
    held = command()
    _hold(store, clock, held)
    request_id = UUID(store.load().requested_correlation_ids[0])
    out = _decide(
        store, clock, _decided(request_id, (), refused=(held.correlation_id,))
    )
    assert isinstance(out, ModelDevRollReleased)
    assert [c.correlation_id for c in out.commands] == [held.correlation_id]


def test_decided_in_mode_off_releases_nothing(store, clock) -> None:
    held = command()
    _hold(store, clock, held)
    request_id = UUID(store.load().requested_correlation_ids[0])
    out = HandlerDevRollGateDecided(
        store, policy=gate_policy(mode="off"), clock=clock
    ).handle(_decided(request_id, (held.correlation_id,)))
    assert isinstance(out, ModelDevRollGateObserved)
    assert out.reason is EnumDevRollGateObservedReason.NOT_LIVE


def _release_one(store, clock):
    held = command()
    _hold(store, clock, held)
    request_id = UUID(store.load().requested_correlation_ids[0])
    _decide(store, clock, _decided(request_id, (held.correlation_id,)))
    return held


def _completed(held, status=EnumRedeployStatus.SUCCESS, **fields):
    return ModelDeployRebuildCompleted(
        correlation_id=str(held.correlation_id), status=status, **fields
    )


def test_a_successful_completion_finishes_the_roll_healthy(store, clock) -> None:
    held = _release_one(store, clock)
    clock.advance(120)
    out = HandlerDevRollGateCompleted(store, policy=gate_policy(), clock=clock).handle(
        _completed(held)
    )
    assert isinstance(out, ModelDevRollGateObserved)
    assert out.reason is EnumDevRollGateObservedReason.ROLL_FINISHED
    state = store.load()
    assert state.outstanding == ()
    assert state.last_roll is not None
    assert state.last_roll.health is EnumDevRollHealth.HEALTHY
    assert state.last_roll.finished_at == clock.now
    assert state.last_roll.healthy_since == clock.now


def test_a_failed_completion_finishes_the_roll_unhealthy(store, clock) -> None:
    held = _release_one(store, clock)
    HandlerDevRollGateCompleted(store, policy=gate_policy(), clock=clock).handle(
        _completed(held, EnumRedeployStatus.FAILED)
    )
    last = store.load().last_roll
    assert last is not None
    assert last.health is EnumDevRollHealth.UNHEALTHY
    assert last.healthy_since is None
    assert last.finished_at is not None


def test_the_roll_finishes_when_the_last_of_several_commands_is_answered(
    store, clock
) -> None:
    one, two = command(), command()
    _hold(store, clock, one)
    _hold(store, clock, two)
    request_id = UUID(store.load().requested_correlation_ids[1])
    _decide(
        store, clock, _decided(request_id, (one.correlation_id, two.correlation_id))
    )
    rejected = HandlerDevRollGateRejected(store, policy=gate_policy(), clock=clock)
    first = rejected.handle(
        ModelDeployRebuildRejected(
            correlation_id=one.correlation_id,
            reason=EnumDeployRejectionReason.SUPERSEDED,
            scope="full",
            superseded_by_sha="d" * 40,
            superseded_by_correlation_id=two.correlation_id,
        )
    )
    assert isinstance(first, ModelDevRollGateObserved)
    assert first.reason is EnumDevRollGateObservedReason.ROLL_PENDING
    assert store.load().last_roll.finished_at is None
    HandlerDevRollGateCompleted(store, policy=gate_policy(), clock=clock).handle(
        _completed(two)
    )
    last = store.load().last_roll
    assert last.health is EnumDevRollHealth.HEALTHY


def test_a_completion_for_a_command_the_gate_never_released_is_not_its_own(
    store, clock
) -> None:
    out = HandlerDevRollGateCompleted(store, policy=gate_policy(), clock=clock).handle(
        _completed(command())
    )
    assert isinstance(out, ModelDevRollGateObserved)
    assert out.reason is EnumDevRollGateObservedReason.NOT_OURS


def test_a_finished_roll_with_commands_held_asks_for_the_next_decision(
    store, clock
) -> None:
    held = _release_one(store, clock)
    waiting = command()
    _hold(store, clock, waiting)
    out = HandlerDevRollGateCompleted(store, policy=gate_policy(), clock=clock).handle(
        _completed(held)
    )
    assert isinstance(out, ModelDevRollFacts)
    assert out.last_roll is not None
    assert out.last_roll.finished_at is not None
    assert [e.event_id for e in out.pending_events] == [waiting.correlation_id]


def test_a_roll_the_agent_never_answers_ends_after_the_overlay_bound_unknown_health(
    store, clock
) -> None:
    _release_one(store, clock)
    waiting = command()
    _hold(store, clock, waiting)
    clock.advance(ROLL_BOUND_SECONDS - 1)
    tick = HandlerDevRollGateTick(store, policy=gate_policy(), clock=clock)
    facts = tick.handle(ModelDevRollGateTick(tick_id=uuid4()))
    assert isinstance(facts, ModelDevRollFacts)
    assert facts.last_roll.finished_at is None
    clock.advance(1)
    facts = tick.handle(ModelDevRollGateTick(tick_id=uuid4()))
    assert isinstance(facts, ModelDevRollFacts)
    assert facts.last_roll.finished_at == NOW + timedelta(seconds=ROLL_BOUND_SECONDS)
    assert facts.last_roll.health is EnumDevRollHealth.UNKNOWN


def test_tick_with_nothing_held_says_so(store, clock) -> None:
    out = HandlerDevRollGateTick(store, policy=gate_policy(), clock=clock).handle(
        ModelDevRollGateTick(tick_id=uuid4())
    )
    assert isinstance(out, ModelDevRollGateObserved)
    assert out.reason is EnumDevRollGateObservedReason.NOTHING_HELD


def test_tick_in_mode_off_drains_everything_held_oldest_first(store, clock) -> None:
    one, two = command(), command()
    _hold(store, clock, one)
    clock.advance(5)
    _hold(store, clock, two)
    out = HandlerDevRollGateTick(
        store, policy=gate_policy(mode="off"), clock=clock
    ).handle(ModelDevRollGateTick(tick_id=uuid4()))
    assert isinstance(out, ModelDevRollReleased)
    assert [c.correlation_id for c in out.commands] == [
        one.correlation_id,
        two.correlation_id,
    ]
    assert store.load().held == ()


def test_held_commands_survive_a_restart_of_the_node(tmp_path: Path, clock) -> None:
    """AC1 falsifier: a command lost across a restart. A second store on the same directory reads them."""
    held = [command() for _ in range(3)]
    first = FileDevRollGateStore(tmp_path / "gate")
    for item in held:
        _hold(first, clock, item)
        clock.advance(1)
    restarted = FileDevRollGateStore(tmp_path / "gate")
    assert [h.command.correlation_id for h in restarted.load().held] == [
        item.correlation_id for item in held
    ]
    request_id = UUID(restarted.load().requested_correlation_ids[-1])
    out = _decide(
        restarted,
        clock,
        _decided(request_id, tuple(i.correlation_id for i in held)),
    )
    assert isinstance(out, ModelDevRollReleased)
    assert [c.correlation_id for c in out.commands] == [i.correlation_id for i in held]


def test_a_state_file_that_is_broken_is_refused_not_read_as_empty(
    tmp_path: Path,
) -> None:
    (tmp_path / "gate").mkdir()
    (tmp_path / "gate" / "gate-state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        FileDevRollGateStore(tmp_path / "gate").load()
