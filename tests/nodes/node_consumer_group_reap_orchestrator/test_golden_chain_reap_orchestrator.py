# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains through the orchestrator: census, state fold, record, command, outcome (OMN-23992).

Every hop crosses the in-memory bus as JSON, as the runtime wires the four nodes.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.consumer_group_reap import (
    EnumReapRefusal,
    ModelGroupReapCommand,
    ModelGroupReapOutcome,
)
from omnimarket.models.consumer_group_state import ModelGroupStateRecord
from omnimarket.nodes.node_consumer_group_reap_effect.handlers.handler_group_reap import (
    HandlerGroupReap,
)
from omnimarket.nodes.node_consumer_group_reap_orchestrator.handlers.handler_group_reap_orchestrator import (
    REAP_COMMAND_TOPIC,
    GroupReapRecordInconsistentError,
    HandlerGroupReapOrchestrator,
    command_id,
    decide,
)
from omnimarket.nodes.node_consumer_group_state_reducer.handlers.handler_group_state import (
    fold,
)
from tests.nodes.node_consumer_group_reap_effect.support import (
    COMPLETED_TOPIC,
    GROUP_A,
    GROUP_B,
    STABLE,
    FakeBroker,
    build_handler,
    census,
    due_record,
    policy,
)

pytestmark = pytest.mark.unit

STATE_TOPIC = "onex.evt.omnimarket.consumer-group-state-updated.v1"
LIVE = {GROUP_A: "EMPTY", GROUP_B: "EMPTY", STABLE: "STABLE"}


async def wire(
    bus: EventBusInmemory, reap: HandlerGroupReap
) -> HandlerGroupReapOrchestrator:
    """Subscribe the orchestrator to the state topic and the reap effect to the command topic."""
    orchestrator = HandlerGroupReapOrchestrator()

    async def on_record(message: Any) -> None:
        record = ModelGroupStateRecord.model_validate_json(message.value)
        output = orchestrator.handle(record)
        for envelope in output.events:
            await bus.publish(
                envelope.event_type,
                key=None,
                value=envelope.payload.model_dump_json().encode(),
            )

    async def on_command(message: Any) -> None:
        received = ModelGroupReapCommand.model_validate_json(message.value)
        outcome = await reap.handle(received)
        await bus.publish(
            COMPLETED_TOPIC, key=None, value=outcome.model_dump_json().encode()
        )

    await bus.start()
    await bus.subscribe(STATE_TOPIC, on_message=on_record, group_id="test-orchestrator")
    await bus.subscribe(REAP_COMMAND_TOPIC, on_message=on_command, group_id="test-reap")
    return orchestrator


async def outcomes(bus: EventBusInmemory) -> list[ModelGroupReapOutcome]:
    history = await bus.get_event_history(topic=COMPLETED_TOPIC)
    return [ModelGroupReapOutcome.model_validate_json(m.value) for m in history]


async def test_golden_chain_two_censuses_to_a_read_back_outcome(
    event_bus: EventBusInmemory,
) -> None:
    broker = FakeBroker(LIVE)
    first = fold(None, census(LIVE, 0), policy())
    record = fold(first, census(LIVE, policy().empty_since_bound_seconds + 1), policy())
    await wire(event_bus, build_handler(broker))
    try:
        await event_bus.publish(
            STATE_TOPIC, key=None, value=record.model_dump_json().encode()
        )
        done = await outcomes(event_bus)
    finally:
        await event_bus.close()
    assert len(done) == 1
    assert done[0].status == "reaped"
    assert set(done[0].read_back_absent) == {GROUP_A, GROUP_B}
    assert broker.groups == {STABLE: "STABLE"}


async def test_a_record_with_nothing_due_publishes_no_command(
    event_bus: EventBusInmemory,
) -> None:
    broker = FakeBroker(LIVE)
    record = fold(None, census(LIVE, 0), policy())
    assert record.reap_due == ()
    await wire(event_bus, build_handler(broker))
    try:
        await event_bus.publish(
            STATE_TOPIC, key=None, value=record.model_dump_json().encode()
        )
        commands = await event_bus.get_event_history(topic=REAP_COMMAND_TOPIC)
        done = await outcomes(event_bus)
    finally:
        await event_bus.close()
    assert commands == []
    assert done == []
    assert broker.delete_calls == []


def test_the_command_is_built_from_the_record_and_carries_its_digest() -> None:
    record = due_record(GROUP_A, GROUP_B)
    command = decide(record)
    assert command is not None
    assert command.decided_by == "reducer"
    assert command.decision_digest == record.decision_digest
    assert [g.name for g in command.groups] == list(record.reap_due)
    assert command.correlation_id == command_id(record)


def test_the_same_record_builds_the_same_command_id_and_another_census_another() -> (
    None
):
    record = due_record(GROUP_A)
    assert command_id(record) == command_id(record.model_copy())
    other = record.model_copy(update={"census_correlation_id": uuid4()})
    assert command_id(other) != command_id(record)


def test_a_record_naming_a_due_group_it_does_not_hold_is_refused() -> None:
    record = due_record(GROUP_A)
    broken = record.model_copy(update={"reap_due": (*record.reap_due, GROUP_B)})
    with pytest.raises(GroupReapRecordInconsistentError):
        decide(broken)


async def test_error_chain_a_record_whose_due_list_was_edited_is_refused_by_the_effect(
    event_bus: EventBusInmemory,
) -> None:
    """Control: a due list edited after the fold still builds a command, and the digest check stops the delete."""
    broker = FakeBroker(LIVE)
    record = due_record(GROUP_A)
    edited = record.model_copy(
        update={
            "reap_due": (GROUP_A, STABLE),
            "groups": tuple(
                g.model_copy(update={"empty_since": record.updated_at})
                if g.name == STABLE
                else g
                for g in record.groups
            ),
        }
    )
    await wire(event_bus, build_handler(broker))
    try:
        await event_bus.publish(
            STATE_TOPIC, key=None, value=edited.model_dump_json().encode()
        )
        done = await outcomes(event_bus)
    finally:
        await event_bus.close()
    assert len(done) == 1
    assert done[0].status == "refused"
    assert EnumReapRefusal.DIGEST_MISMATCH in {r.code for r in done[0].refusals}
    assert broker.delete_calls == []
    assert broker.groups == LIVE


async def test_error_chain_a_delete_failure_ends_failed_on_the_completed_topic(
    event_bus: EventBusInmemory,
) -> None:
    broker = FakeBroker(LIVE)
    broker.fail = {GROUP_A}
    await wire(event_bus, build_handler(broker))
    try:
        await event_bus.publish(
            STATE_TOPIC,
            key=None,
            value=due_record(GROUP_A).model_dump_json().encode(),
        )
        done = await outcomes(event_bus)
    finally:
        await event_bus.close()
    assert len(done) == 1
    assert done[0].status == "failed"
    assert done[0].delete_errors == {GROUP_A: "BROKER_ERROR"}
