# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events: census, state fold, reap command, outcome on the in-memory bus (OMN-23992)."""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_consumer_group_reap_effect.handlers.handler_group_reap import (
    HandlerGroupReap,
)
from omnimarket.nodes.node_consumer_group_reap_effect.models import (
    EnumReapRefusal,
    ModelGroupReapCommand,
    ModelGroupReapOutcome,
)
from omnimarket.nodes.node_consumer_group_state_reducer.models import (
    ModelGroupStateRecord,
)
from tests.nodes.node_consumer_group_reap_effect.support import (
    COMMAND_TOPIC,
    COMPLETED_TOPIC,
    GROUP_A,
    GROUP_B,
    STABLE,
    FakeBroker,
    build_handler,
    command,
    due_record,
)

pytestmark = pytest.mark.unit

LIVE = {GROUP_A: "EMPTY", GROUP_B: "EMPTY", STABLE: "STABLE"}


async def chain(
    bus: EventBusInmemory, handler: HandlerGroupReap, cmd: ModelGroupReapCommand
) -> ModelGroupReapOutcome:
    """The command crosses the bus as JSON, the handler consumes it, the outcome crosses back."""

    async def on_command(message: object) -> None:
        received = ModelGroupReapCommand.model_validate_json(message.value)  # type: ignore[attr-defined]
        outcome = await handler.handle(received)
        await bus.publish(
            COMPLETED_TOPIC, key=None, value=outcome.model_dump_json().encode()
        )

    await bus.start()
    await bus.subscribe(
        COMMAND_TOPIC, on_message=on_command, group_id="test-consumer-group-reap"
    )
    await bus.publish(COMMAND_TOPIC, key=None, value=cmd.model_dump_json().encode())
    history = await bus.get_event_history(topic=COMPLETED_TOPIC)
    assert len(history) == 1
    return ModelGroupReapOutcome.model_validate_json(history[0].value)


async def test_golden_chain_reducer_record_to_read_back_outcome(
    event_bus: EventBusInmemory,
) -> None:
    broker = FakeBroker(LIVE)
    record = ModelGroupStateRecord.model_validate_json(
        due_record(GROUP_A, GROUP_B).model_dump_json()
    )
    try:
        outcome = await chain(event_bus, build_handler(broker), command(record))
    finally:
        await event_bus.close()
    assert outcome.status == "reaped"
    assert set(outcome.read_back_absent) == {GROUP_A, GROUP_B}
    assert broker.groups == {STABLE: "STABLE"}


async def test_error_chain_file_decided_command_ends_refused_on_the_completed_topic(
    event_bus: EventBusInmemory,
) -> None:
    broker = FakeBroker(LIVE)
    file_decided = command(due_record(GROUP_A)).model_copy(
        update={"decided_by": "file"}
    )
    try:
        outcome = await chain(event_bus, build_handler(broker), file_decided)
    finally:
        await event_bus.close()
    assert outcome.status == "refused"
    assert {r.code for r in outcome.refusals} == {
        EnumReapRefusal.DECIDED_NOT_BY_REDUCER
    }
    assert broker.groups == LIVE


async def test_error_chain_delete_failure_ends_failed_on_the_completed_topic(
    event_bus: EventBusInmemory,
) -> None:
    broker = FakeBroker(LIVE)
    broker.fail = {GROUP_A}
    try:
        outcome = await chain(
            event_bus, build_handler(broker), command(due_record(GROUP_A))
        )
    finally:
        await event_bus.close()
    assert outcome.status == "failed"
    assert outcome.delete_errors == {GROUP_A: "BROKER_ERROR"}
