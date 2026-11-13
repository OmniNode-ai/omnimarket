# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the host install state reducer (OMN-23957).

Seam 1 (install workflow to reducer): the step event the workflow publishes is the event the reducer folds. The event crosses a
bus as JSON (in-memory always, Redpanda when ``HOST_INSTALL_PARITY_BOOTSTRAP`` names a lab broker) and the record the reducer
returns is the same record in-process, on the in-memory bus and on the broker.
Seam 2 (registry effect to install workflow): the result of ``node_host_installer_registry_units`` carries the cells the
workflow turns into step events (the rendered unit, the revision, the status), and its request and completed topics stay as
the workflow addresses them. The test reads the effect's own models and contract; neither side is stubbed.
Control: a renamed cell on the event crossing the bus is refused by the reducer's model, and a registry result with a cell
renamed no longer carries what the workflow reads.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from typing import Any, get_args

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    fold,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallState,
    ModelHostInstallRecord,
    ModelHostInstallStepEvent,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)
from tests.nodes.node_host_install_state_reducer.support import (
    NODE_DIR,
    error_chain,
    golden_chain,
    overlay,
)

pytestmark = pytest.mark.unit

BOOTSTRAP_ENV = "HOST_INSTALL_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
REGISTRY_DIR = NODE_DIR.parent / "node_host_installer_registry_units"
STEP_TOPIC = "onex.evt.omnimarket.host-install-step.v1"
STATE_TOPIC = "onex.evt.omnimarket.host-install-state-updated.v1"


def consume(request: dict[str, Any]) -> dict[str, Any]:
    """The consumer side: what the reducer does with a step event that arrived as JSON."""
    prior = request["prior"]
    try:
        event = ModelHostInstallStepEvent.model_validate(request["event"])
        record = fold(
            ModelHostInstallRecord.model_validate(prior) if prior else None,
            event,
            overlay(),
        )
    except ValueError as refusal:
        return {"id": request["id"], "refused": type(refusal).__name__}
    return {"id": request["id"], "record": json.loads(record.model_dump_json())}


def chain_over(
    call: Callable[[dict[str, Any]], dict[str, Any]], chain: list[Any]
) -> dict[str, Any]:
    """Send each step of a chain through ``call``, carrying the record from one reply to the next."""
    prior: dict[str, Any] | None = None
    reply: dict[str, Any] = {}
    for item in chain:
        reply = call(
            {
                "id": uuid.uuid4().hex,
                "prior": prior,
                "event": json.loads(item.model_dump_json()),
            }
        )
        if "record" not in reply:
            return reply
        prior = reply["record"]
    return reply


def settled(reply: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in reply.items() if k != "id"}


class Bus:
    async def start(self) -> None: ...
    async def call(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="host-install-parity", group="parity")
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        await self.bus.start()

        async def serve(message: Any) -> None:
            await self.bus.publish(
                STATE_TOPIC,
                None,
                json.dumps(consume(json.loads(message.value))).encode(),
            )

        async def reply_to(message: Any) -> None:
            reply = json.loads(message.value)
            self.waiting.pop(reply["id"]).set_result(reply)

        await self.bus.subscribe(STEP_TOPIC, on_message=serve, group_id="reducer")
        await self.bus.subscribe(STATE_TOPIC, on_message=reply_to, group_id="reader")

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.bus.publish(STEP_TOPIC, None, json.dumps(payload).encode())
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaBus(Bus):
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.prefix = f"host-install-parity-{uuid.uuid4().hex}"
        self.tasks: list[asyncio.Task[None]] = []
        self.consumers: list[Any] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, security_protocol="PLAINTEXT"
        )
        await self.producer.start()

        async def run_consumer(
            topic: str, handle: Callable[[dict[str, Any]], Any]
        ) -> None:
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=self.bootstrap,
                security_protocol="PLAINTEXT",
                group_id=f"{self.prefix}-{topic}",
                auto_offset_reset="earliest",
            )
            await consumer.start()
            self.consumers.append(consumer)
            async for message in consumer:
                await handle(json.loads(message.value))

        async def serve(request: dict[str, Any]) -> None:
            await self.producer.send_and_wait(
                f"{self.prefix}.state", json.dumps(consume(request)).encode()
            )

        async def reply_to(reply: dict[str, Any]) -> None:
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        self.tasks.append(
            asyncio.create_task(run_consumer(f"{self.prefix}.step", serve))
        )
        self.tasks.append(
            asyncio.create_task(run_consumer(f"{self.prefix}.state", reply_to))
        )
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.producer.send_and_wait(
            f"{self.prefix}.step", json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for consumer in self.consumers:
            await consumer.stop()
        await self.producer.stop()


async def over(bus: Bus, chain: list[Any]) -> dict[str, Any]:
    await bus.start()
    try:
        prior: dict[str, Any] | None = None
        reply: dict[str, Any] = {}
        for item in chain:
            reply = await bus.call(
                {
                    "id": uuid.uuid4().hex,
                    "prior": prior,
                    "event": json.loads(item.model_dump_json()),
                }
            )
            if "record" not in reply:
                return reply
            prior = reply["record"]
        return reply
    finally:
        await bus.stop()


def in_process(chain: list[Any]) -> dict[str, Any]:
    return chain_over(consume, chain)


def test_parity_in_process_golden_and_error_chains_read_their_final_states() -> None:
    assert in_process(golden_chain())["record"]["state"] == EnumHostInstallState.RUN_OK
    assert (
        in_process(error_chain())["record"]["state"] == EnumHostInstallState.ROLLED_BACK
    )


@pytest.mark.host_install_parity
def test_parity_in_memory_bus_answers_what_the_in_process_read_answers() -> None:
    for chain in (golden_chain(), error_chain()):
        reply = asyncio.run(over(InmemoryBus(), chain))
        assert settled(reply) == settled(in_process(chain))


@pytest.mark.host_install_parity
def test_parity_a_renamed_cell_crossing_the_in_memory_bus_is_refused() -> None:
    class Renaming(InmemoryBus):
        async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
            event = dict(payload["event"])
            event["state"] = event.pop("outcome")
            return await super().call({**payload, "event": event})

    reply = asyncio.run(over(Renaming(), golden_chain()))
    assert reply.get("refused") == "ValidationError"
    assert settled(reply) != settled(in_process(golden_chain()))


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.host_install_parity
def test_parity_redpanda_bus_answers_what_the_in_memory_bus_answers() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    for chain in (golden_chain(), error_chain()):
        broker = asyncio.run(over(RedpandaBus(os.environ[BOOTSTRAP_ENV]), chain))
        memory = asyncio.run(over(InmemoryBus(), chain))
        assert settled(broker) == settled(memory) == settled(in_process(chain))


# --- seam 2: the registry effect the workflow installs through -------------------------------------------------------

#: The cells of the registry effect's result the install workflow turns into step events.
WORKFLOW_READS = {
    "unit",
    "mode",
    "status",
    "exit_code",
    "rendered",
    "revision",
    "bundle_sha256",
    "record",
}
REQUEST_TOPIC = "onex.cmd.omnimarket.host-registry-unit-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.host-registry-unit-completed.v1"


def test_seam_registry_result_carries_the_cells_the_workflow_reads() -> None:
    assert set(ModelRegistryUnitsResult.model_fields) >= WORKFLOW_READS
    assert ModelRegistryUnitsResult.model_fields["rendered"].annotation is str
    assert ModelRegistryUnitsResult.model_fields["exit_code"].annotation is int


def test_seam_registry_request_takes_the_modes_the_workflow_sends() -> None:
    modes = get_args(ModelRegistryUnitsRequest.model_fields["mode"].annotation)
    assert {"dry-run", "install"} <= set(modes)
    assert {"unit", "home", "registry_root"} <= set(
        ModelRegistryUnitsRequest.model_fields
    )


def test_seam_registry_topics_are_the_ones_the_workflow_addresses() -> None:
    doc = yaml.safe_load((REGISTRY_DIR / "contract.yaml").read_text())
    assert doc["runtime_dispatch"]["command_topic"] == REQUEST_TOPIC
    assert doc["terminal_event"] == COMPLETED_TOPIC


def test_control_a_registry_result_with_a_renamed_cell_loses_what_the_workflow_reads() -> (
    None
):
    renamed = set(ModelRegistryUnitsResult.model_fields) - {"rendered"} | {"render"}
    assert not renamed >= WORKFLOW_READS
