# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A released roll ends on the completion of any command of its batch, not only of the newest.

The dev deploy agent that runs a command publishes its completion; the agent a command is routed away from
skips it and publishes nothing. A batch whose newest command is one the agent skipped therefore never got the
completion the gate waited for, held every later merge until the completion deadline and was abandoned.

The replay below is the shape of the batch of 2026-11-23T23:38Z: the roll of one command abandoned at the
deadline with no completion, then twelve held commands released at once, a mix of commands the agent runs and
commands it skips, the newest of them one it skips. The ids are synthetic; the recorded mix is not readable
from this repository, so the count, the interleaving and the times here are the brief's, not a copy of a log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.events.runtime_deployment import (
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.models.dev_roll import EnumDevRollHealth, ModelDevRollRecord
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateRollOutcome,
    ModelDevRollGateRollRecorded,
    ModelDevRollGateState,
    ModelDevRollWindowGatePolicy,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    POLICY,
    Clock,
    Gate,
    completed,
    sha,
)

pytestmark = pytest.mark.unit

DEADLINE = 3600
INCIDENT_POLICY: ModelDevRollWindowGatePolicy = POLICY.model_copy(
    update={"roll_completion_deadline_seconds": DEADLINE, "held_kept": 200}
)
ABANDONED_JOB = "c7fff070"
ROLL_STARTED = datetime(2026, 11, 23, 22, 38, 35, tzinfo=UTC)
HOLD_AT = datetime(2026, 11, 23, 23, 38, 0, tzinfo=UTC)
TICK_AT = datetime(2026, 11, 23, 23, 38, 36, tzinfo=UTC)
# requesting repository of each of the twelve commands, oldest first; the newest is a command the agent skips
MIX = (
    "omnimarket",
    "omnimarket",
    "omnibase_infra",
    "omnimarket",
    "omnimarket",
    "omnibase_infra",
    "omnimarket",
    "omnibase_infra",
    "omnimarket",
    "omnimarket",
    "omnibase_infra",
    "omnimarket",
)
RUNS_HERE = (2, 5, 7, 10)  # positions in MIX of the commands the agent runs


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="replay",
        tick_interval_ms=1000,
    )


def released_batch() -> tuple[Gate, list[UUID]]:
    """The roll of ABANDONED_JOB is overdue, twelve commands are held; one tick abandons it and releases all twelve."""
    gate = Gate(INCIDENT_POLICY, Clock(HOLD_AT))
    gate.store.save(
        ModelDevRollGateState(
            last_roll=ModelDevRollRecord(
                job_id=ABANDONED_JOB,
                git_ref=sha(900),
                runtime_started_at=ROLL_STARTED,
                finished_at=None,
                health=EnumDevRollHealth.UNKNOWN,
                healthy_since=None,
            )
        )
    )
    ids: list[UUID] = []
    for position, repository in enumerate(MIX):
        command_id = uuid4()
        ids.append(command_id)
        gate.hold.handle(
            ModelDeployPublishCommand(
                correlation_id=command_id,
                git_ref=sha(position + 1),
                runtime_lane=EnumRuntimeLane.DEV,
                requested_by=f"gha/{repository}/pr-{100 + position}",
                requested_at=HOLD_AT - timedelta(minutes=30 - position),
            )
        )
    gate.clock.now = TICK_AT
    out = gate.tick.handle(tick(TICK_AT))
    sent = [m for m in out or [] if isinstance(m, ModelDeployPublishCommand)]
    assert [m.correlation_id for m in sent] == ids
    state = gate.store.load()
    assert state.held == ()
    assert state.last_roll is not None
    assert state.last_roll.job_id == str(ids[-1])
    assert [a.command_correlation_id for a in state.abandoned] == [ABANDONED_JOB]
    return gate, ids


def test_roll_gate_batch_completion_ends_the_roll_on_a_command_the_agent_ran() -> None:
    gate, ids = released_batch()
    ran = ids[RUNS_HERE[0]]
    finished_at = TICK_AT + timedelta(minutes=4)
    recorded = gate.completed.handle(completed(ran, at=finished_at))
    assert isinstance(recorded, ModelDevRollGateRollRecorded)
    assert recorded.outcome is EnumDevRollGateRollOutcome.FINISHED_HEALTHY
    assert recorded.command_correlation_id == str(ran)
    last = gate.store.load().last_roll
    assert last is not None
    assert last.job_id == str(ids[-1])  # the roll keeps the newest command as its id
    assert last.finished_at == finished_at
    assert last.health is EnumDevRollHealth.HEALTHY
    # the deadline passes with the roll already finished: it is not abandoned a second time
    gate.clock.now = TICK_AT + timedelta(seconds=DEADLINE + 60)
    out = gate.tick.handle(tick(gate.clock.now))
    assert not [
        m
        for m in out or []
        if isinstance(m, ModelDevRollGateRollRecorded)
        and m.outcome is EnumDevRollGateRollOutcome.ABANDONED
    ]
    assert len(gate.store.load().abandoned) == 1


def test_roll_gate_batch_completion_of_the_newest_command_still_ends_the_roll() -> None:
    gate, ids = released_batch()
    finished_at = TICK_AT + timedelta(minutes=4)
    recorded = gate.completed.handle(completed(ids[-1], at=finished_at))
    assert isinstance(recorded, ModelDevRollGateRollRecorded)
    assert recorded.outcome is EnumDevRollGateRollOutcome.FINISHED_HEALTHY


def test_roll_gate_batch_completion_outside_the_batch_changes_nothing() -> None:
    gate, ids = released_batch()
    before = gate.store.load()
    stranger = uuid4()
    for stray in (stranger, ABANDONED_JOB):  # never released / the abandoned roll's job
        assert (
            gate.completed.handle(completed(stray, at=TICK_AT + timedelta(minutes=4)))
            is None
        )
    assert gate.store.load() == before
    assert ids  # the batch itself was released


def test_roll_gate_batch_completion_of_a_command_still_held_changes_nothing() -> None:
    gate, _ = released_batch()
    held_id = uuid4()
    gate.hold.handle(
        ModelDeployPublishCommand(
            correlation_id=held_id,
            git_ref=sha(77),
            runtime_lane=EnumRuntimeLane.DEV,
            requested_by="gha/omnimarket/pr-777",
            requested_at=TICK_AT,
        )
    )
    before = gate.store.load()
    assert before.held
    assert (
        gate.completed.handle(completed(held_id, at=TICK_AT + timedelta(minutes=4)))
        is None
    )
    assert gate.store.load() == before


def test_roll_gate_batch_completion_after_an_abandon_is_not_the_next_rolls() -> None:
    """A late completion of a command of an abandoned roll's batch must not end the roll that replaced it."""
    gate, old_ids = released_batch()
    gate.clock.now = TICK_AT + timedelta(seconds=DEADLINE + 1)
    gate.tick.handle(tick(gate.clock.now))  # abandons the unfinished second roll
    assert gate.store.load().last_roll is None
    for number in (1, 2):
        gate.hold.handle(
            ModelDeployPublishCommand(
                correlation_id=uuid4(),
                git_ref=sha(500 + number),
                runtime_lane=EnumRuntimeLane.DEV,
                requested_by="gha/omnimarket/pr-900",
                requested_at=gate.clock.now - timedelta(minutes=30),
            )
        )
    gate.clock.advance(900)
    gate.tick.handle(tick(gate.clock.now))
    before = gate.store.load()
    assert before.last_roll is not None
    assert before.last_roll.finished_at is None
    assert (
        gate.completed.handle(completed(old_ids[RUNS_HERE[1]], at=gate.clock.now))
        is None
    )
    assert gate.store.load() == before


def test_roll_gate_batch_completion_a_later_completion_of_the_batch_sets_the_final_health() -> (
    None
):
    """An agent that runs an unfoldable batch in order completes the newest last: its health is the roll's health."""
    gate, ids = released_batch()
    early = TICK_AT + timedelta(minutes=4)
    first = gate.completed.handle(completed(ids[RUNS_HERE[0]], at=early))
    assert isinstance(first, ModelDevRollGateRollRecorded)
    assert first.outcome is EnumDevRollGateRollOutcome.FINISHED_HEALTHY
    # the same completion again, and an earlier one, change nothing
    before = gate.store.load()
    assert gate.completed.handle(completed(ids[RUNS_HERE[0]], at=early)) is None
    assert (
        gate.completed.handle(
            completed(ids[RUNS_HERE[1]], at=early - timedelta(seconds=30))
        )
        is None
    )
    assert gate.store.load() == before
    later = early + timedelta(minutes=3)
    recorded = gate.completed.handle(
        completed(ids[-1], at=later, status=EnumRedeployStatus.FAILED)
    )
    assert isinstance(recorded, ModelDevRollGateRollRecorded)
    assert recorded.outcome is EnumDevRollGateRollOutcome.FINISHED_UNHEALTHY
    last = gate.store.load().last_roll
    assert last is not None
    assert last.finished_at == later
    assert last.health is EnumDevRollHealth.UNHEALTHY


def test_roll_gate_batch_completion_state_written_before_the_batch_was_kept_still_ends_on_the_newest() -> (
    None
):
    """A state saved by the earlier code has an in-flight roll and no batch ids: its newest command's completion ends it."""
    gate, ids = released_batch()
    state = gate.store.load()
    legacy = ModelDevRollGateState.model_validate(
        state.model_dump(mode="json", exclude={"last_roll_command_ids"})
    )
    gate.store.save(legacy)
    recorded = gate.completed.handle(
        completed(ids[-1], at=TICK_AT + timedelta(minutes=4))
    )
    assert isinstance(recorded, ModelDevRollGateRollRecorded)
    assert recorded.outcome is EnumDevRollGateRollOutcome.FINISHED_HEALTHY
