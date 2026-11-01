"""The effect command to the census terminal over the bus: a planted census over the threshold ends FAIL (OMN-21818)."""

from collections.abc import Mapping

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.consumer_group_guard import (
    ModelConsumerGroupCensus,
    ModelConsumerGroupGuardRequest,
)
from omnimarket.nodes.node_consumer_group_guard_effect import (
    HandlerConsumerGroupGuardEffect,
)

pytestmark = pytest.mark.unit

COMMAND = "onex.cmd.omnimarket.consumer-group-guard-effect-requested.v1"
TERMINAL = "onex.evt.omnimarket.consumer-group-guard-effect-completed.v1"


async def test_effect_command_to_census_terminal(event_bus: EventBusInmemory):
    def read(_config: Mapping[str, str]) -> dict[str, int]:
        return {"EMPTY": 19154, "STABLE": 311}

    handler = HandlerConsumerGroupGuardEffect(
        read, lambda _lane: ("dev-broker", "local", {})
    )

    async def on_command(message):
        request = ModelConsumerGroupGuardRequest.model_validate_json(message.value)
        census = await handler.handle(request)
        await event_bus.publish(
            TERMINAL, key=None, value=census.model_dump_json().encode()
        )

    await event_bus.start()
    try:
        await event_bus.subscribe(
            COMMAND, on_message=on_command, group_id="test-consumer-group-guard"
        )
        await event_bus.publish(
            COMMAND,
            key=None,
            value=ModelConsumerGroupGuardRequest(bus_lane="dev")
            .model_dump_json()
            .encode(),
        )
        history = await event_bus.get_event_history(topic=TERMINAL)
        assert len(history) == 1
        census = ModelConsumerGroupCensus.model_validate_json(history[0].value)
        assert census.status == "FAIL"
        assert census.groups_empty == 19154
        assert census.error == ""
    finally:
        await event_bus.close()
