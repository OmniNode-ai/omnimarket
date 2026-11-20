# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain, error chain, join and seam of the dev roll window gate on the real broker (OMN-24438).

The in-memory tests run the gate's real contract through the runtime's own dispatch. These run the same chain
over the integration lane's broker: the three subscribed topics and the five published ones are the
contract's, every message is a ``ModelEventEnvelope`` as the runtime reads it, and each handler answers what
it decoded. It skips where the integration lane's broker is not reachable (the ``integration_kafka_bootstrap`` fixture).

Each seam test sends one broken message and asserts the gate never answers it, beside a positive control that
sends the same message unbroken and asserts the answer, so a silent consumer cannot pass the seam tests:
a bare payload with no envelope, a hold command with ``requested_by`` renamed, a completion with
``correlation_id`` renamed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.events.runtime_deployment import (
    ModelDeployPublishCommand,
    ModelDeployRebuildCompleted,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateDecision,
    ModelDevRollGateHeld,
    ModelDevRollGateRefused,
    ModelDevRollGateRollRecorded,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    COMPLETED_TOPIC as CONTRACT_COMPLETED_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    DECISION_TOPIC as CONTRACT_DECISION_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    DEPLOY_PUBLISH_TOPIC as CONTRACT_DEPLOY_PUBLISH_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    HELD_TOPIC as CONTRACT_HELD_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    HOLD_TOPIC as CONTRACT_HOLD_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    KEY,
    envelope,
    tick_payload,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    RECORDED_TOPIC as CONTRACT_RECORDED_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    REFUSED_TOPIC as CONTRACT_REFUSED_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    TICK_TOPIC as CONTRACT_TICK_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    WINDOW,
    Gate,
    command,
    completed,
)

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

# The topics are the contract's, each with a suffix of this run: the gate's own topics on a shared broker may
# have a deployed gate reading them, and a test command there would be held and released toward the deploy
# agent. The in-memory tests read the contract's own names; the suffix is the only difference here.
RUN = uuid.uuid4().hex[:8]


def scoped(topic: str) -> str:
    return f"{topic}.it-{RUN}"


COMPLETED_TOPIC = scoped(CONTRACT_COMPLETED_TOPIC)
DECISION_TOPIC = scoped(CONTRACT_DECISION_TOPIC)
DEPLOY_PUBLISH_TOPIC = scoped(CONTRACT_DEPLOY_PUBLISH_TOPIC)
HELD_TOPIC = scoped(CONTRACT_HELD_TOPIC)
HOLD_TOPIC = scoped(CONTRACT_HOLD_TOPIC)
RECORDED_TOPIC = scoped(CONTRACT_RECORDED_TOPIC)
REFUSED_TOPIC = scoped(CONTRACT_REFUSED_TOPIC)
TICK_TOPIC = scoped(CONTRACT_TICK_TOPIC)

TIMEOUT_SECONDS = 20.0
QUIET_SECONDS = 3.0
OUTPUT_TOPICS: dict[type, str] = {
    ModelDeployPublishCommand: DEPLOY_PUBLISH_TOPIC,
    ModelDevRollGateHeld: HELD_TOPIC,
    ModelDevRollGateRefused: REFUSED_TOPIC,
    ModelDevRollGateDecision: DECISION_TOPIC,
    ModelDevRollGateRollRecorded: RECORDED_TOPIC,
}


class Wire:
    """The gate's three handlers behind real topics, and a reader on each of its five answers.

    A consumer group is new for each run and reads from the start, so a message of an earlier run is on the
    topic too: only a message whose envelope carries a correlation id this run sent is answered or read.
    """

    def __init__(self, bus: EventBusKafka) -> None:
        self.bus = bus
        self.gate = Gate()
        self.run = uuid.uuid4().hex[:8]
        self.mine: set[str] = set()
        self.seen: dict[str, list[Any]] = {
            topic: [] for topic in OUTPUT_TOPICS.values()
        }
        self.failures: list[str] = []
        self.undecodable = 0
        self._unsubscribe: list[Any] = []

    async def open(self) -> None:
        inbound = (
            (HOLD_TOPIC, ModelDeployPublishCommand, self.gate.hold.handle),
            (TICK_TOPIC, ModelRuntimeTick, self.gate.tick.handle),
            (COMPLETED_TOPIC, ModelDeployRebuildCompleted, self.gate.completed.handle),
        )
        for topic, model, handle in inbound:
            self._unsubscribe.append(
                await self.bus.subscribe(
                    topic=topic,
                    group_id=f"dev-roll-gate-{topic}-{self.run}",
                    on_message=self._answer(model, handle),
                )
            )
        for topic in OUTPUT_TOPICS.values():
            self._unsubscribe.append(
                await self.bus.subscribe(
                    topic=topic,
                    group_id=f"dev-roll-gate-read-{topic}-{self.run}",
                    on_message=self._collect(topic),
                )
            )
        await self.bus.start_consuming()

    async def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            await unsubscribe()

    def _answer(self, model: type[BaseModel], handle: Any) -> Any:
        async def on_message(message: Any) -> None:
            try:
                received = ModelEventEnvelope[object].model_validate_json(message.value)
            except ValueError:
                self.undecodable += (
                    1  # the runtime dead-letters a message with no envelope
                )
                return
            if str(received.correlation_id) not in self.mine:
                return
            try:
                outputs = handle(model.model_validate(received.payload))
            except ValueError as exc:  # a payload the handler's model refuses
                self.failures.append(str(exc))
                return
            outputs = (
                []
                if outputs is None
                else [outputs]
                if isinstance(outputs, BaseModel)
                else list(outputs)
            )
            for output in outputs:
                await self.bus.publish(
                    topic=OUTPUT_TOPICS[type(output)],
                    key=KEY,
                    value=envelope(
                        output.model_dump(mode="json"),
                        type(output).__name__,
                        received.correlation_id,
                    ),
                )

        return on_message

    def _collect(self, topic: str) -> Any:
        async def on_message(message: Any) -> None:
            received = ModelEventEnvelope[object].model_validate_json(message.value)
            if str(received.correlation_id) in self.mine:
                self.seen[topic].append(received.payload)

        return on_message

    async def send(
        self, topic: str, payload: dict[str, object], payload_type: str, key: uuid.UUID
    ) -> None:
        self.mine.add(str(key))
        await self.bus.publish(
            topic=topic, key=KEY, value=envelope(payload, payload_type, key)
        )

    async def send_bare(
        self, topic: str, payload: dict[str, object], key: uuid.UUID
    ) -> None:
        self.mine.add(str(key))
        await self.bus.publish(
            topic=topic, key=KEY, value=json.dumps(payload).encode("utf-8")
        )

    async def hold(self, item: ModelDeployPublishCommand) -> None:
        await self.send(
            HOLD_TOPIC,
            item.model_dump(mode="json"),
            "ModelDeployPublishCommand",
            item.correlation_id,
        )

    async def tick(self) -> None:
        await self.send(
            TICK_TOPIC,
            tick_payload(self.gate.clock.now),
            "ModelRuntimeTick",
            uuid.uuid4(),
        )

    async def arrived(self, topic: str, count: int) -> list[Any]:
        async def wait() -> None:
            while len(self.seen[topic]) < count:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait(), timeout=TIMEOUT_SECONDS)
        return self.seen[topic][:count]

    async def stays_empty(self, topic: str) -> None:
        await asyncio.sleep(QUIET_SECONDS)
        assert self.seen[topic] == []


@pytest_asyncio.fixture
async def wire(integration_kafka_bootstrap: str) -> AsyncGenerator[Wire, None]:
    bus = EventBusKafka(
        config=ModelKafkaEventBusConfig(
            bootstrap_servers=integration_kafka_bootstrap,
            environment="integration-test",
            timeout_seconds=10,
            max_retry_attempts=1,
            retry_backoff_base=0.1,
            circuit_breaker_threshold=5,
            circuit_breaker_reset_timeout=30.0,
            consumer_sleep_interval=0.05,
            enable_idempotence=False,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            dead_letter_topic=None,
            instance_id=None,
            reconnect_backoff_ms=500,
            reconnect_backoff_max_ms=2000,
        )
    )
    await bus.start()
    opened = Wire(bus)
    await opened.open()
    try:
        yield opened
    finally:
        await opened.close()
        await bus.close()


async def test_golden_chain_three_commands_held_then_released_together_in_order(
    wire: Wire,
) -> None:
    sent = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2, 3)]
    for item in sent:
        await wire.hold(item)
    held = [
        ModelDevRollGateHeld.model_validate(p)
        for p in await wire.arrived(HELD_TOPIC, 3)
    ]
    assert sorted(h.held_count for h in held) == [1, 2, 3]
    await wire.stays_empty(DEPLOY_PUBLISH_TOPIC)

    wire.gate.clock.advance(WINDOW)
    await wire.tick()
    out = [
        ModelDeployPublishCommand.model_validate(p)
        for p in await wire.arrived(DEPLOY_PUBLISH_TOPIC, 3)
    ]
    assert [c.correlation_id for c in out] == [c.correlation_id for c in sent]
    assert all(c.roll_window_released for c in out)
    (decision,) = [
        ModelDevRollGateDecision.model_validate(p)
        for p in await wire.arrived(DECISION_TOPIC, 1)
    ]
    assert decision.action == "roll_now"
    assert decision.released_count == 3

    wire.gate.clock.advance(300)
    done = completed(sent[-1].correlation_id, at=wire.gate.clock.now)
    await wire.send(
        COMPLETED_TOPIC,
        done.model_dump(mode="json"),
        "ModelDeployRebuildCompleted",
        sent[-1].correlation_id,
    )
    (recorded,) = [
        ModelDevRollGateRollRecorded.model_validate(p)
        for p in await wire.arrived(RECORDED_TOPIC, 1)
    ]
    assert recorded.outcome == "finished_healthy"


async def test_golden_chain_a_tick_before_the_window_releases_nothing(
    wire: Wire,
) -> None:
    await wire.hold(command(number=1, age_seconds=10))
    await wire.arrived(HELD_TOPIC, 1)
    wire.gate.clock.advance(WINDOW // 3)
    await wire.tick()
    (decision,) = [
        ModelDevRollGateDecision.model_validate(p)
        for p in await wire.arrived(DECISION_TOPIC, 1)
    ]
    assert decision.action == "hold"
    assert decision.released_count == 0
    await wire.stays_empty(DEPLOY_PUBLISH_TOPIC)


async def test_error_chain_a_command_the_gate_cannot_hold_is_refused_and_passed_on(
    wire: Wire,
) -> None:
    item = command(number=1, requested_by="someone-by-hand")
    await wire.hold(item)
    (refused,) = [
        ModelDevRollGateRefused.model_validate(p)
        for p in await wire.arrived(REFUSED_TOPIC, 1)
    ]
    assert refused.reason == "requester_not_a_ci_issuer"
    (passed,) = [
        ModelDeployPublishCommand.model_validate(p)
        for p in await wire.arrived(DEPLOY_PUBLISH_TOPIC, 1)
    ]
    assert passed.correlation_id == item.correlation_id
    assert passed.roll_window_released is True
    await wire.stays_empty(HELD_TOPIC)


async def test_join_the_same_command_sent_twice_is_held_once(wire: Wire) -> None:
    item = command(number=1, age_seconds=10)
    await wire.hold(item)
    await wire.arrived(HELD_TOPIC, 1)
    await wire.hold(item)
    held = [
        ModelDevRollGateHeld.model_validate(p)
        for p in await wire.arrived(HELD_TOPIC, 2)
    ]
    assert [h.held_count for h in held] == [1, 1]
    wire.gate.clock.advance(WINDOW)
    await wire.tick()
    await wire.arrived(DEPLOY_PUBLISH_TOPIC, 1)
    await asyncio.sleep(QUIET_SECONDS)
    assert len(wire.seen[DEPLOY_PUBLISH_TOPIC]) == 1


async def test_seam_positive_control_an_enveloped_command_is_held(wire: Wire) -> None:
    await wire.hold(command(number=1, age_seconds=10))
    await wire.arrived(HELD_TOPIC, 1)
    assert wire.failures == []


async def test_seam_break_a_bare_payload_with_no_envelope_is_never_held(
    wire: Wire,
) -> None:
    item = command(number=1, age_seconds=10)
    await wire.send_bare(HOLD_TOPIC, item.model_dump(mode="json"), item.correlation_id)
    await wire.stays_empty(HELD_TOPIC)
    assert (
        wire.failures == []
    )  # refused before the envelope's correlation id is read: nothing answered


async def test_seam_break_a_renamed_requested_by_is_never_held(wire: Wire) -> None:
    item = command(number=1, age_seconds=10)
    body = item.model_dump(mode="json")
    body["requester"] = body.pop("requested_by")
    await wire.send(HOLD_TOPIC, body, "ModelDeployPublishCommand", item.correlation_id)
    await wire.stays_empty(HELD_TOPIC)
    await wire.stays_empty(DEPLOY_PUBLISH_TOPIC)
    assert len(wire.failures) == 1
    assert "requester" in wire.failures[0]


async def test_seam_break_a_renamed_completion_correlation_id_ends_no_roll(
    wire: Wire,
) -> None:
    item = command(number=1, age_seconds=WINDOW)
    await wire.hold(item)
    await wire.arrived(HELD_TOPIC, 1)
    await wire.tick()
    await wire.arrived(DEPLOY_PUBLISH_TOPIC, 1)
    wire.gate.clock.advance(300)
    body = completed(item.correlation_id, at=wire.gate.clock.now).model_dump(
        mode="json"
    )
    body["job_id"] = body.pop("correlation_id")
    await wire.send(
        COMPLETED_TOPIC, body, "ModelDeployRebuildCompleted", item.correlation_id
    )
    await wire.stays_empty(RECORDED_TOPIC)
    last_roll = wire.gate.store.load().last_roll
    assert last_roll is not None
    assert last_roll.finished_at is None
    assert len(wire.failures) == 1
    assert "correlation_id" in wire.failures[0]
