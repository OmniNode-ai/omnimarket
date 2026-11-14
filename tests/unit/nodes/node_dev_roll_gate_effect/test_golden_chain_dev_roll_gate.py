# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_roll_gate_effect over its real contract and the batching node's, on the in-memory bus.

Held commands in, nothing released while the window is open, one release of every held
command oldest first once it ends, the next roll waits for the previous one to finish.
Topic bytes in, typed models out through the runtime's own path: discovery reads each
``contract.yaml``, ``build_routing_map`` resolves each subscribed topic to its handler
and ``event_model``, ``RuntimeDispatch`` decodes the envelope and the handler's return
goes out on the topic ``published_events`` names.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    EnumDevRollHoldReason,
    ModelDevRollDecided,
    ModelDevRollFacts,
    ModelDevRollHoldRequest,
    ModelDevRollReleased,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.store_dev_roll_gate import (
    InMemoryDevRollGateStore,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
)
from tests.unit.nodes.node_dev_roll_gate_effect.chain_support import (
    DECIDED_TOPIC,
    FACTS_TOPIC,
    OBSERVED_TOPIC,
    RELEASED_TOPIC,
    gate_chain,
    gate_handlers,
    join_chain,
    on,
    pump,
    released_ids,
    send_completed,
    send_hold,
    send_tick,
)
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import (
    MIN_INTERVAL_SECONDS,
    WINDOW_SECONDS,
    Clock,
    command,
)

pytestmark = pytest.mark.unit


def _decisions(broker) -> list[ModelDevRollDecided]:
    return [
        ModelDevRollDecided.model_validate(m.payload) for m in on(broker, DECIDED_TOPIC)
    ]


def test_the_subscribed_topics_are_the_contracts_five(store, clock) -> None:
    _dispatch, _transport, _broker, topics = gate_chain(store, clock)
    assert len(topics) == 5
    assert DECIDED_TOPIC in topics


async def test_golden_a_hold_publishes_one_decision_request_and_releases_nothing(
    store, clock
) -> None:
    dispatch, transport, broker, _ = gate_chain(store, clock)
    held = command()
    await send_hold(transport, held)
    assert await pump(dispatch) == 1
    (facts_message,) = on(broker, FACTS_TOPIC)
    facts = ModelDevRollFacts.model_validate(facts_message.payload)
    (event,) = facts.pending_events
    assert event.event_id == held.correlation_id
    assert event.repository == "omnimarket"
    assert event.sha == held.git_ref
    assert event.merged_at == clock.now
    assert facts.last_roll is None
    assert on(broker, RELEASED_TOPIC) == []
    assert on(broker, OBSERVED_TOPIC) == []
    assert [h.command.correlation_id for h in store.load().held] == [
        held.correlation_id
    ]


async def test_golden_nothing_is_released_before_the_window_ends_then_all_together_oldest_first(
    store, clock, compute
) -> None:
    dispatch, transport, broker, _ = join_chain(store, clock, compute)
    first, second, third = command(), command(), command()
    for held, gap in ((first, 0), (second, 100), (third, 100)):
        clock.advance(gap)
        await send_hold(transport, held)
        await pump(dispatch)
    assert on(broker, RELEASED_TOPIC) == []
    assert {d.hold_reason for d in _decisions(broker)} == {
        EnumDevRollHoldReason.WINDOW_OPEN
    }

    # One second before the window of the OLDEST command ends: still nothing.
    clock.now = first_held_at(store) + timedelta(seconds=WINDOW_SECONDS - 1)
    await send_tick(transport)
    await pump(dispatch)
    assert on(broker, RELEASED_TOPIC) == []

    clock.advance(1)
    await send_tick(transport)
    await pump(dispatch)
    assert released_ids(broker) == [
        [
            str(first.correlation_id),
            str(second.correlation_id),
            str(third.correlation_id),
        ]
    ]
    assert store.load().held == ()
    (rolled,) = [
        d for d in _decisions(broker) if d.action is EnumDevRollAction.ROLL_NOW
    ]
    assert len(rolled.rolled_event_ids) == 3


def first_held_at(store: InMemoryDevRollGateStore):
    return min(h.held_at for h in store.load().held)


async def test_golden_a_command_held_after_a_release_waits_for_the_previous_roll_and_the_interval(
    store, clock, compute
) -> None:
    dispatch, transport, broker, _ = join_chain(store, clock, compute)
    one = command()
    await send_hold(transport, one)
    await pump(dispatch)
    clock.advance(WINDOW_SECONDS)
    await send_tick(transport)
    await pump(dispatch)
    assert released_ids(broker) == [[str(one.correlation_id)]]
    roll_started = clock.now

    # A new command's window ends while the previous roll has no completion: no release.
    two = command()
    await send_hold(transport, two)
    await pump(dispatch)
    clock.advance(WINDOW_SECONDS)
    await send_tick(transport)
    await pump(dispatch)
    assert len(released_ids(broker)) == 1
    assert any(
        d.hold_reason is EnumDevRollHoldReason.ROLL_IN_PROGRESS
        for d in _decisions(broker)
    )

    # The roll completes; the minimum start interval still holds the next one.
    await send_completed(transport, one.correlation_id, started_at=roll_started)
    await pump(dispatch)
    assert len(released_ids(broker)) == 1
    clock.now = roll_started + timedelta(seconds=MIN_INTERVAL_SECONDS - 1)
    await send_tick(transport)
    await pump(dispatch)
    assert len(released_ids(broker)) == 1
    assert _decisions(broker)[-1].hold_reason in (
        EnumDevRollHoldReason.MIN_START_INTERVAL,
        EnumDevRollHoldReason.PREVIOUS_ROLL_NOT_HEALTHY,
    )

    clock.now = roll_started + timedelta(seconds=MIN_INTERVAL_SECONDS + 1)
    await send_tick(transport)
    await pump(dispatch)
    assert released_ids(broker) == [
        [str(one.correlation_id)],
        [str(two.correlation_id)],
    ]


class _ReleasesEverythingAtOnce:
    """Failing control: a gate that passes every command through at once."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def handle(self, request: ModelDevRollHoldRequest) -> ModelDevRollReleased:
        return ModelDevRollReleased(
            release_id=request.command.correlation_id,
            decision_correlation_id=None,
            released_at=self._clock(),
            commands=(request.command,),
        )


async def test_failing_control_a_gate_that_releases_at_once_fails_the_window_expectation(
    store, clock, compute
) -> None:
    """The golden expectation (nothing released inside the window) is red against this stand-in."""
    handlers = gate_handlers(store, clock)
    handlers["HandlerDevRollGateHold"] = _ReleasesEverythingAtOnce(clock)
    dispatch, transport, broker, _ = join_chain(store, clock, compute, handlers)
    await send_hold(transport, command())
    await pump(dispatch)
    assert on(broker, RELEASED_TOPIC) != [], (
        "the stand-in released inside the window, which the golden test above forbids"
    )


class _NeverReleases:
    """Failing control: a gate that answers every decision as not its own."""

    def handle(self, request: ModelDevRollDecided) -> ModelDevRollGateObserved:
        return ModelDevRollGateObserved(reason=EnumDevRollGateObservedReason.NOT_OURS)


async def test_failing_control_a_gate_that_never_releases_fails_the_release_expectation(
    store, clock, compute
) -> None:
    handlers = gate_handlers(store, clock)
    handlers["HandlerDevRollGateDecided"] = _NeverReleases()
    dispatch, transport, broker, _ = join_chain(store, clock, compute, handlers)
    await send_hold(transport, command())
    await pump(dispatch)
    clock.advance(WINDOW_SECONDS)
    await send_tick(transport)
    await pump(dispatch)
    assert released_ids(broker) == [], "the release the golden test expects never came"
