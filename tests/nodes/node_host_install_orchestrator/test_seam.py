# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the whole install over a bus, on the in-memory bus and on the real broker (OMN-23957).

Every payload crosses the bus as JSON and is decoded by the topic's own model, so a cell renamed on either side of a
topic fails here. The same chain runs in process (``Plant``), over the in-memory bus, and over a Redpanda broker (the
leg runs only where ``HOST_INSTALL_PARITY_BOOTSTRAP`` names a plaintext lab broker); all three must end the same way.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

from omnimarket.nodes.node_host_install_file_effect.models.model_host_install_file import (
    ModelHostInstallFileRequest,
    ModelHostInstallFileResult,
)
from omnimarket.nodes.node_host_install_orchestrator.handlers.handler_host_install_orchestrator import (
    TOPIC_COMPLETED,
    TOPIC_FILE_REQUESTED,
    TOPIC_READBACK_REQUESTED,
    TOPIC_REGISTRY_REQUESTED,
    TOPIC_STEP,
    HandlerHostInstallOrchestrator,
)
from omnimarket.nodes.node_host_install_orchestrator.models import (
    ModelHostInstallCompleted,
    ModelHostInstallRequest,
)
from omnimarket.nodes.node_host_install_readback_compute.models.model_host_install_readback import (
    ModelHostInstallReadbackRequest,
    ModelHostInstallReadbackResult,
)
from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    HostInstallTransitionError,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)
from tests.nodes.node_host_install_file_effect.support import FakeHostInstallFiles
from tests.nodes.node_host_install_orchestrator.support import (
    NEW_TIMER,
    OLD_SERVICE,
    OLD_TIMER,
    SERVICE_PATH,
    TIMER_PATH,
    Plant,
    StepClock,
    orchestrator_overlay,
    request,
)

pytestmark = pytest.mark.unit

BOOTSTRAP_ENV = "HOST_INSTALL_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
REQUEST_TOPIC = "onex.cmd.omnimarket.host-install-requested.v1"
REGISTRY_DONE = "onex.evt.omnimarket.host-registry-unit-completed.v1"
FILE_DONE = "onex.evt.omnimarket.host-install-file-completed.v1"
READBACK_DONE = "onex.evt.omnimarket.host-install-readback-completed.v1"
STATE_TOPIC = "onex.evt.omnimarket.host-install-state-updated.v1"
MISMATCHED_TIMER = "[Timer]\nOnActiveSec=77s\n"

# Each topic and the typed model that decodes what crosses it.
MODELS: dict[str, type[BaseModel]] = {
    REQUEST_TOPIC: ModelHostInstallRequest,
    TOPIC_REGISTRY_REQUESTED: ModelRegistryUnitsRequest,
    REGISTRY_DONE: ModelRegistryUnitsResult,
    TOPIC_FILE_REQUESTED: ModelHostInstallFileRequest,
    FILE_DONE: ModelHostInstallFileResult,
    TOPIC_READBACK_REQUESTED: ModelHostInstallReadbackRequest,
    READBACK_DONE: ModelHostInstallReadbackResult,
    TOPIC_STEP: ModelHostInstallReduceCommand,
    STATE_TOPIC: ModelHostInstallRecord,
    TOPIC_COMPLETED: ModelHostInstallCompleted,
}

Callback = Callable[[str, bytes], Awaitable[None]]


class Wire:
    async def start(
        self,
        topics: list[str],
        on_message: Callable[[str, str, bytes], Awaitable[None]],
    ) -> None: ...
    async def publish(self, topic: str, key: str, value: bytes) -> None: ...
    async def stop(self) -> None: ...


class InmemoryWire(Wire):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="host-install-seam", group="seam")

    async def start(
        self,
        topics: list[str],
        on_message: Callable[[str, str, bytes], Awaitable[None]],
    ) -> None:
        await self.bus.start()
        for topic in topics:

            async def serve(message: Any, topic: str = topic) -> None:
                await on_message(topic, (message.key or b"").decode(), message.value)

            await self.bus.subscribe(topic, on_message=serve, group_id=f"seam-{topic}")

    async def publish(self, topic: str, key: str, value: bytes) -> None:
        await self.bus.publish(topic, key.encode(), value)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaWire(Wire):
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.prefix = f"host-install-seam-{uuid.uuid4().hex}"
        self.tasks: list[asyncio.Task[None]] = []
        self.consumers: list[Any] = []

    def name(self, topic: str) -> str:
        return f"{self.prefix}.{topic}"

    async def start(
        self,
        topics: list[str],
        on_message: Callable[[str, str, bytes], Awaitable[None]],
    ) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, security_protocol="PLAINTEXT"
        )
        await self.producer.start()

        async def run_consumer(topic: str) -> None:
            consumer = AIOKafkaConsumer(
                self.name(topic),
                bootstrap_servers=self.bootstrap,
                security_protocol="PLAINTEXT",
                group_id=f"{self.prefix}-{topic}",
                auto_offset_reset="earliest",
            )
            await consumer.start()
            self.consumers.append(consumer)
            async for message in consumer:
                await on_message(topic, (message.key or b"").decode(), message.value)

        for topic in topics:
            self.tasks.append(asyncio.create_task(run_consumer(topic)))
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def publish(self, topic: str, key: str, value: bytes) -> None:
        await self.producer.send_and_wait(self.name(topic), value, key=key.encode())

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for consumer in self.consumers:
            await consumer.stop()
        await self.producer.stop()


class BusPlant:
    """The nodes the orchestrator composes, each on its own topics, reached only through the wire."""

    def __init__(self, wire: Wire, plant: Plant) -> None:
        self.wire = wire
        self.plant = plant
        self.done: asyncio.Future[ModelHostInstallCompleted] = (
            asyncio.get_event_loop().create_future()
        )

    async def on_message(self, topic: str, key: str, value: bytes) -> None:
        payload = MODELS[topic].model_validate_json(value)
        if topic == TOPIC_COMPLETED:
            assert isinstance(payload, ModelHostInstallCompleted)
            self.plant.completed = payload
            self.done.set_result(payload)
            return
        if topic in (
            REQUEST_TOPIC,
            REGISTRY_DONE,
            FILE_DONE,
            READBACK_DONE,
            STATE_TOPIC,
        ):
            for out in self.plant.orchestrator.decide(payload, uuid.UUID(key)):
                await self.wire.publish(
                    out.topic, key, out.payload.model_dump_json().encode()
                )
            return
        try:
            answer = self.plant._serve(topic, payload)
        except HostInstallTransitionError as refusal:
            self.plant.refusals.append(str(refusal))
            return
        if answer is None:
            return
        done_topic = {
            TOPIC_REGISTRY_REQUESTED: REGISTRY_DONE,
            TOPIC_FILE_REQUESTED: FILE_DONE,
            TOPIC_READBACK_REQUESTED: READBACK_DONE,
            TOPIC_STEP: STATE_TOPIC,
        }[topic]
        assert isinstance(answer, BaseModel)
        await self.wire.publish(done_topic, key, answer.model_dump_json().encode())


def fresh(*, install_timer: str = NEW_TIMER, install_ok: bool = True) -> Plant:
    files = FakeHostInstallFiles()
    files.seed(SERVICE_PATH, OLD_SERVICE)
    files.seed(TIMER_PATH, OLD_TIMER)
    return Plant(
        HandlerHostInstallOrchestrator(orchestrator_overlay(), StepClock()),
        files,
        install_timer=install_timer,
        install_ok=install_ok,
    )


async def over(wire: Wire, plant: Plant) -> ModelHostInstallCompleted:
    bus = BusPlant(wire, plant)
    await wire.start(list(MODELS), bus.on_message)
    try:
        start = request()
        await wire.publish(
            REQUEST_TOPIC, str(start.correlation_id), start.model_dump_json().encode()
        )
        return await asyncio.wait_for(bus.done, TIMEOUT_S)
    finally:
        await wire.stop()


def in_process(*, install_timer: str = NEW_TIMER, install_ok: bool = True) -> Plant:
    plant = fresh(install_timer=install_timer, install_ok=install_ok)
    plant.run(request())
    return plant


def states(plant: Plant) -> list[str]:
    return [r.state.value for r in plant.records]


@pytest.mark.host_install_parity
@pytest.mark.parametrize(
    ("install_timer", "install_ok"),
    [(NEW_TIMER, True), (MISMATCHED_TIMER, True), (NEW_TIMER, False)],
    ids=["golden", "readback-mismatch-rolls-back", "install-failure-rolls-back"],
)
def test_the_in_memory_bus_ends_every_chain_as_the_in_process_run_does(
    install_timer: str, install_ok: bool
) -> None:
    expected = in_process(install_timer=install_timer, install_ok=install_ok)
    assert expected.completed is not None
    plant = fresh(install_timer=install_timer, install_ok=install_ok)
    completed = asyncio.run(over(InmemoryWire(), plant))
    assert completed.model_dump(
        exclude={"correlation_id"}
    ) == expected.completed.model_dump(exclude={"correlation_id"})
    assert states(plant) == states(expected)
    assert plant.refusals == []


@pytest.mark.host_install_parity
def test_a_renamed_cell_crossing_the_in_memory_bus_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "TIMEOUT_S", 3.0)

    class Renaming(InmemoryWire):
        async def publish(self, topic: str, key: str, value: bytes) -> None:
            if topic == FILE_DONE:
                value = value.replace(b'"digest"', b'"hash"')
            await super().publish(topic, key, value)

    plant = fresh()
    with pytest.raises(TimeoutError):
        asyncio.run(over(Renaming(), plant))
    assert plant.completed is None


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.host_install_parity
def test_the_redpanda_bus_ends_every_chain_as_the_in_memory_bus_does() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    for install_timer in (NEW_TIMER, MISMATCHED_TIMER):
        memory = fresh(install_timer=install_timer)
        in_mem = asyncio.run(over(InmemoryWire(), memory))
        broker_plant = fresh(install_timer=install_timer)
        on_broker = asyncio.run(
            over(RedpandaWire(os.environ[BOOTSTRAP_ENV]), broker_plant)
        )
        assert on_broker.model_dump(exclude={"correlation_id"}) == in_mem.model_dump(
            exclude={"correlation_id"}
        )
        assert states(broker_plant) == states(memory)
