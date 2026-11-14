# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of node_dev_roll_gate_effect (OMN-24431 AC4).

Seam 1 (deploy command to gate): the fields the gate reads from the deploy command.
Seam 2 (gate to batching node): the facts the gate publishes are the facts the batching node reads, and the
decision fields the gate reads are the ones the batching node writes.
Seam 3 (deploy agent to gate): the completion and rejection fields the gate reads.
Seam 4 (topics): each topic one node publishes is the topic its counterpart subscribes.
Seam 5 (parity): the hold request and the decision cross a bus as JSON (in-memory always, Redpanda when
``DEV_ROLL_GATE_PARITY_BOOTSTRAP`` names a lab broker) and the gate answers the same as in process.
Every list below is spelled in this repository, so a rename on either side turns a test red; each has a control.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.runtime_deployment import (
    ModelDeployPublishCommand,
    ModelDeployRebuildCompleted,
    ModelDeployRebuildRejected,
)
from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    ModelDevRollDecided,
    ModelDevRollFacts,
    ModelDevRollHoldRequest,
    ModelDevRollReleased,
    ModelDevRollRequest,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers import (
    HandlerDevRollGateDecided,
    HandlerDevRollGateHold,
    InMemoryDevRollGateStore,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD_REQUESTED,
)
from tests.unit.nodes.node_dev_roll_gate_effect.chain_support import (
    COMPLETED_TOPIC,
    COMPUTE_CONTRACT,
    DECIDED_TOPIC,
    FACTS_TOPIC,
    HOLD_TOPIC,
    REJECTED_TOPIC,
    RELEASED_TOPIC,
)
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import (
    NOW,
    POLICY,
    Clock,
    command,
)

pytestmark = pytest.mark.unit

#: What the gate reads from the deploy command, and the type it reads it as.
GATE_READS_FROM_COMMAND = {
    "correlation_id",
    "requested_by",
    "git_ref",
    "requested_at",
    "runtime_lane",
}
#: What the batching node reads from the facts, and what the gate writes into them.
FACTS_FIELDS = {"correlation_id", "observed_at", "pending_events", "last_roll"}
TIP_FIELDS = {"event_id", "repository", "sha", "merged_at"}
#: What the gate reads from the batching node's decision.
GATE_READS_FROM_DECISION = {
    "correlation_id",
    "action",
    "hold_reason",
    "rolled_event_ids",
    "refused_events",
    "would_request",
}
#: What the gate reads from the deploy agent's completion and rejection events.
GATE_READS_FROM_COMPLETED = {"correlation_id", "status", "started_at", "completed_at"}
GATE_READS_FROM_REJECTED = {"correlation_id", "reason"}


def _fields(model: type) -> set[str]:
    return set(model.model_fields)


def test_seam_the_gate_reads_fields_the_deploy_command_has() -> None:
    assert _fields(ModelDeployPublishCommand) >= GATE_READS_FROM_COMMAND


def test_seam_the_facts_the_gate_publishes_are_the_facts_the_batching_node_reads() -> (
    None
):
    assert _fields(ModelDevRollFacts) == FACTS_FIELDS
    assert _fields(ModelDevTipChanged) == TIP_FIELDS
    store, clock = InMemoryDevRollGateStore(), Clock()
    out = HandlerDevRollGateHold(store, policy=POLICY, clock=clock).handle(
        ModelDevRollHoldRequest(command=command())
    )
    assert isinstance(out, ModelDevRollFacts)
    assert set(json.loads(out.model_dump_json())) == FACTS_FIELDS


def test_seam_the_gate_reads_fields_the_decision_has() -> None:
    assert _fields(ModelDevRollDecided) >= GATE_READS_FROM_DECISION
    assert "git_ref" in _fields(ModelDevRollRequest)


def test_seam_the_gate_reads_fields_the_agents_completion_and_rejection_have() -> None:
    assert _fields(ModelDeployRebuildCompleted) >= GATE_READS_FROM_COMPLETED
    assert _fields(ModelDeployRebuildRejected) >= GATE_READS_FROM_REJECTED


def test_seam_the_released_event_carries_the_commands_the_deploy_effect_publishes() -> (
    None
):
    assert _fields(ModelDevRollReleased) == {
        "release_id",
        "decision_correlation_id",
        "released_at",
        "commands",
    }
    assert (
        ModelDevRollReleased.model_fields["commands"].annotation
        == tuple[ModelDeployPublishCommand, ...]
    )


def test_seam_each_topic_one_node_publishes_is_the_one_its_counterpart_subscribes() -> (
    None
):
    compute = yaml.safe_load(COMPUTE_CONTRACT.read_text(encoding="utf-8"))
    assert HOLD_TOPIC == TOPIC_DEV_ROLL_HOLD_REQUESTED
    assert FACTS_TOPIC in compute["event_bus"]["subscribe_topics"]
    assert compute["runtime_dispatch"]["terminal_events"]["success"] == DECIDED_TOPIC
    deploy = yaml.safe_load(
        (
            COMPUTE_CONTRACT.parents[1] / "node_redeploy_deploy_effect/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert RELEASED_TOPIC in deploy["event_bus"]["subscribe_topics"]
    assert HOLD_TOPIC in deploy["event_bus"]["publish_topics"]
    assert COMPLETED_TOPIC in deploy["event_bus"]["subscribe_topics"]
    assert REJECTED_TOPIC in deploy["event_bus"]["subscribe_topics"]


@pytest.mark.parametrize(
    ("model", "reads", "renamed"),
    [
        (ModelDeployPublishCommand, GATE_READS_FROM_COMMAND, "correlation_id"),
        (ModelDevRollDecided, GATE_READS_FROM_DECISION, "rolled_event_ids"),
        (ModelDeployRebuildCompleted, GATE_READS_FROM_COMPLETED, "status"),
        (ModelDeployRebuildRejected, GATE_READS_FROM_REJECTED, "correlation_id"),
    ],
)
def test_control_a_renamed_field_loses_what_the_gate_reads(
    model: type, reads: set[str], renamed: str
) -> None:
    after_rename = _fields(model) - {renamed} | {renamed + "_x"}
    assert not after_rename >= reads


def test_control_a_renamed_facts_field_no_longer_matches_what_the_batching_node_reads() -> (
    None
):
    assert (
        _fields(ModelDevRollFacts) - {"pending_events"} | {"pending"}
    ) != FACTS_FIELDS


# --- seam 5: parity of the hold and the decision across a bus ------------------------------------------------------

BOOTSTRAP_ENV = "DEV_ROLL_GATE_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
REQUEST_TOPIC = "dev-roll-gate-parity.request"
REPLY_TOPIC = "dev-roll-gate-parity.reply"
GATE_NOW = NOW


def consume(request: dict[str, Any]) -> dict[str, Any]:
    """The consumer side: the gate over the JSON that arrived; a fresh gate per chain."""
    try:
        store = _STORES.setdefault(request["chain"], InMemoryDevRollGateStore())
        clock = Clock(GATE_NOW)
        if request["op"] == "hold":
            held = ModelDevRollHoldRequest.model_validate(request["payload"])
            out = HandlerDevRollGateHold(store, policy=POLICY, clock=clock).handle(held)
        else:
            decided = ModelDevRollDecided.model_validate(request["payload"])
            out = HandlerDevRollGateDecided(store, policy=POLICY, clock=clock).handle(
                decided
            )
    except ValueError as refusal:
        return {"id": request["id"], "refused": type(refusal).__name__}
    return {"id": request["id"], "out": json.loads(out.model_dump_json())}


_STORES: dict[str, InMemoryDevRollGateStore] = {}


def _decided(request_id: uuid.UUID, rolled: tuple[uuid.UUID, ...]) -> dict[str, Any]:
    return json.loads(
        ModelDevRollDecided(
            correlation_id=request_id,
            observed_at=GATE_NOW,
            action=EnumDevRollAction.ROLL_NOW,
            hold_reason=None,
            hold_until=None,
            shadow=True,
            would_request=ModelDevRollRequest(
                correlation_id=request_id,
                requested_by="test",
                scope="full",
                runtime_lane="dev",
                git_ref="c" * 40,
                requested_at=GATE_NOW,
            ),
            rolled_event_ids=rolled,
            newest_sha_by_repository={},
            oldest_pending_merged_at=None,
            longest_wait_seconds=0,
            refused_events=(),
        ).model_dump_json()
    )


def steps(chain: str) -> list[dict[str, Any]]:
    """Two holds, then the decision that rolls both: the golden chain, as bus messages."""
    one, two = command(), command()
    # the facts id of the second hold is derived from the command and the clock, as the gate derives it
    facts_id = ModelDevRollFacts.model_validate(
        HandlerDevRollGateHold(
            InMemoryDevRollGateStore(), policy=POLICY, clock=Clock(GATE_NOW)
        )
        .handle(ModelDevRollHoldRequest(command=two))
        .model_dump()
    ).correlation_id
    return [
        {
            "chain": chain,
            "op": "hold",
            "payload": json.loads(
                ModelDevRollHoldRequest(command=one).model_dump_json()
            ),
        },
        {
            "chain": chain,
            "op": "hold",
            "payload": json.loads(
                ModelDevRollHoldRequest(command=two).model_dump_json()
            ),
        },
        {
            "chain": chain,
            "op": "decided",
            "payload": _decided(facts_id, (one.correlation_id, two.correlation_id)),
        },
    ]


def settled(replies: list[dict[str, Any]]) -> list[str]:
    """What each reply says, without the ids that differ run to run."""
    summary: list[str] = []
    for reply in replies:
        out = reply.get("out")
        if out is None:
            summary.append(f"refused:{reply['refused']}")
        elif "commands" in out:
            summary.append(f"released:{len(out['commands'])}")
        elif "pending_events" in out:
            summary.append(f"facts:{len(out['pending_events'])}")
        else:
            summary.append(f"observed:{out['reason']}")
    return summary


class Bus:
    async def start(self) -> None: ...
    async def call(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="dev-roll-gate-parity", group="parity")
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        await self.bus.start()

        async def serve(message: Any) -> None:
            await self.bus.publish(
                REPLY_TOPIC,
                None,
                json.dumps(consume(json.loads(message.value))).encode(),
            )

        async def reply_to(message: Any) -> None:
            reply = json.loads(message.value)
            self.waiting.pop(reply["id"]).set_result(reply)

        await self.bus.subscribe(REQUEST_TOPIC, on_message=serve, group_id="gate")
        await self.bus.subscribe(REPLY_TOPIC, on_message=reply_to, group_id="reader")

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.bus.publish(REQUEST_TOPIC, None, json.dumps(payload).encode())
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaBus(Bus):
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.prefix = f"dev-roll-gate-parity-{uuid.uuid4().hex}"
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
                f"{self.prefix}.reply", json.dumps(consume(request)).encode()
            )

        async def reply_to(reply: dict[str, Any]) -> None:
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        self.tasks.append(
            asyncio.create_task(run_consumer(f"{self.prefix}.request", serve))
        )
        self.tasks.append(
            asyncio.create_task(run_consumer(f"{self.prefix}.reply", reply_to))
        )
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.producer.send_and_wait(
            f"{self.prefix}.request", json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for consumer in self.consumers:
            await consumer.stop()
        await self.producer.stop()


async def over(bus: Bus, chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    await bus.start()
    try:
        return [await bus.call({**step, "id": uuid.uuid4().hex}) for step in chain]
    finally:
        await bus.stop()


def in_process(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [consume({**step, "id": uuid.uuid4().hex}) for step in chain]


def test_parity_in_process_the_golden_chain_holds_twice_then_releases_both() -> None:
    assert settled(in_process(steps(uuid.uuid4().hex))) == [
        "facts:1",
        "facts:2",
        "released:2",
    ]


@pytest.mark.dev_roll_gate_parity
def test_parity_in_memory_bus_answers_what_the_in_process_read_answers() -> None:
    chain = steps(uuid.uuid4().hex)
    assert settled(asyncio.run(over(InmemoryBus(), chain))) == settled(
        in_process(steps(uuid.uuid4().hex))
    )


@pytest.mark.dev_roll_gate_parity
def test_parity_a_renamed_field_crossing_the_in_memory_bus_is_refused() -> None:
    class Renaming(InmemoryBus):
        async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
            if payload["op"] == "hold":
                command_doc = dict(payload["payload"]["command"])
                command_doc["correlation"] = command_doc.pop("correlation_id")
                payload = {**payload, "payload": {"command": command_doc}}
            return await super().call(payload)

    replies = asyncio.run(over(Renaming(), steps(uuid.uuid4().hex)))
    assert settled(replies)[0] == "refused:ValidationError"
    assert settled(replies) != ["facts:1", "facts:2", "released:2"]


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.dev_roll_gate_parity
def test_parity_redpanda_bus_answers_what_the_in_memory_bus_answers() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    broker = settled(
        asyncio.run(
            over(RedpandaBus(os.environ[BOOTSTRAP_ENV]), steps(uuid.uuid4().hex))
        )
    )
    memory = settled(asyncio.run(over(InmemoryBus(), steps(uuid.uuid4().hex))))
    assert broker == memory == settled(in_process(steps(uuid.uuid4().hex)))
