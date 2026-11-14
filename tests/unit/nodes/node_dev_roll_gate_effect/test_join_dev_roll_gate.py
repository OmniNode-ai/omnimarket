# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join of node_dev_roll_gate_effect and node_dev_roll_request_compute on one in-memory bus, over two simulated hours.

A merge every four minutes, a tick every thirty seconds, a stand-in deploy agent that
completes each roll after a fixed time. The gate must release at most once per minimum
start interval, never while the previous roll has no completion, never before the
window of the oldest command it releases has ended, and never lose a command.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
from uuid import UUID

import pytest

from omnimarket.models.dev_roll import ModelDevRollHoldRequest, ModelDevRollReleased
from tests.unit.nodes.node_dev_roll_gate_effect.chain_support import (
    RELEASED_TOPIC,
    gate_handlers,
    join_chain,
    on,
    pump,
    send_completed,
    send_hold,
    send_rejected,
    send_tick,
)
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import (
    MIN_INTERVAL_SECONDS,
    NOW,
    WINDOW_SECONDS,
    Clock,
    command,
)

pytestmark = pytest.mark.unit

TWO_HOURS = 7200
MERGE_EVERY = 240
TICK_EVERY = 30
ROLL_SECONDS = 300


class Simulation:
    """What the two-hour run recorded."""

    def __init__(self) -> None:
        self.held_at: dict[UUID, datetime] = {}
        self.releases: list[tuple[datetime, list[UUID]]] = []
        self.completions: list[datetime] = []
        self.completion_before_release: list[bool] = []


async def _run(store, clock, compute, handlers=None) -> Simulation:
    dispatch, transport, broker, _ = join_chain(store, clock, compute, handlers)
    sim = Simulation()
    seen = 0
    due: list[tuple[datetime, list[UUID]]] = []
    for elapsed in range(0, TWO_HOURS + 1, TICK_EVERY):
        clock.now = NOW + timedelta(seconds=elapsed)
        if elapsed % MERGE_EVERY == 0:
            merged = command()
            sim.held_at[merged.correlation_id] = clock.now
            await send_hold(transport, merged)
        await send_tick(transport)
        await pump(dispatch)
        for _, ids in [d for d in due if d[0] <= clock.now]:
            *superseded, newest = ids
            for old in superseded:
                await send_rejected(transport, old, "superseded", superseded_by=newest)
            await send_completed(transport, newest, completed_at=clock.now)
            sim.completions.append(clock.now)
        due = [d for d in due if d[0] > clock.now]
        await pump(dispatch)
        messages = on(broker, RELEASED_TOPIC)
        for message in messages[seen:]:
            released = ModelDevRollReleased.model_validate(message.payload)
            ids = [c.correlation_id for c in released.commands]
            # every roll before this one has a completion at or before this release
            sim.completion_before_release.append(
                len(sim.completions) == len(sim.releases)
            )
            sim.releases.append((clock.now, ids))
            due.append((clock.now + timedelta(seconds=ROLL_SECONDS), ids))
        seen = len(messages)
    return sim


async def test_two_hours_of_a_merge_every_four_minutes_roll_no_more_than_the_interval_allows(
    store, clock, compute
) -> None:
    sim = await _run(store, clock, compute)
    allowed = TWO_HOURS // MIN_INTERVAL_SECONDS + 1
    # every roll ended on its own completion, not on the overlay's unanswered-roll bound
    assert len(sim.releases) >= 3
    assert len(sim.completions) >= len(sim.releases) - 1
    assert 1 <= len(sim.releases) <= allowed, (
        f"{len(sim.releases)} releases in two hours, the interval allows {allowed}"
    )
    stamps = [t for t, _ in sim.releases]
    assert all(
        later - earlier >= timedelta(seconds=MIN_INTERVAL_SECONDS)
        for earlier, later in pairwise(stamps)
    )
    # never a release while the previous roll has no completion
    assert all(sim.completion_before_release)
    # never a release before the window of its oldest command ended
    for released_at, ids in sim.releases:
        oldest = min(sim.held_at[i] for i in ids)
        assert released_at >= oldest + timedelta(seconds=WINDOW_SECONDS)


async def test_two_hours_lose_no_command_and_release_each_once_oldest_first(
    store, clock, compute
) -> None:
    sim = await _run(store, clock, compute)
    released = [i for _, ids in sim.releases for i in ids]
    still_held = [h.command.correlation_id for h in store.load().held]
    assert len(released) == len(set(released))
    assert sorted(released + still_held, key=str) == sorted(sim.held_at, key=str)
    for _, ids in sim.releases:
        assert [sim.held_at[i] for i in ids] == sorted(sim.held_at[i] for i in ids)


class _ReleasesEveryCommand:
    """Failing control: a gate with no batching, one release per merge."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def handle(self, request: ModelDevRollHoldRequest) -> ModelDevRollReleased:
        return ModelDevRollReleased(
            release_id=request.command.correlation_id,
            decision_correlation_id=None,
            released_at=self._clock(),
            commands=(request.command,),
        )


async def test_failing_control_a_gate_with_no_batching_exceeds_the_interval_in_the_same_simulation(
    store, clock, compute
) -> None:
    handlers = gate_handlers(store, clock)
    handlers["HandlerDevRollGateHold"] = _ReleasesEveryCommand(clock)
    sim = await _run(store, clock, compute, handlers)
    allowed = TWO_HOURS // MIN_INTERVAL_SECONDS + 1
    assert len(sim.releases) > allowed
    assert not all(sim.completion_before_release)
