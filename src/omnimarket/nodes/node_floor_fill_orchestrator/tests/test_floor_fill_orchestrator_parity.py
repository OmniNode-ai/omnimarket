# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill orchestrator decides the same on the in-memory bus and on a Redpanda bus (plan T13, G-NODE point 5).

The scenario is four startable lines, one runner that refuses one key, and one degraded read (the pool). Each
case is a JSON message on the bus under test naming the case; the consumer builds the orchestrator on its scripted
seams and a conditional store, runs a ``tick`` and publishes the answer's decision projection (status, exit
code, lines and each key's result) back as a JSON message. The projection of every case is one row of the run, and
the parity run's own comparison (``compare_runs``) judges the in-memory run against the Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names in
``FLOOR_FILL_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the proof, never the dev lane's):

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import EnumFloorFillCommand
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedPool,
    ScriptedStarter,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import harness, overlay

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

BOOTSTRAP_ENV = "FLOOR_FILL_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)
CASES = ("00:four-lines", "01:one-refusing-runner", "02:degraded-read")


def run_case(name: str) -> dict[str, Any]:
    """One tick of the orchestrator on scripted seams; its decision projection."""
    config = overlay(max_fill_lanes_per_line=1)
    if name == "00:four-lines":
        h = harness(config=config)
    elif name == "01:one-refusing-runner":
        h = harness(
            config=config,
            starter=ScriptedStarter(fail_keys=frozenset({"floor:bravo:b1"})),
        )
    else:
        h = harness(config=config, pool=ScriptedPool(5, unreadable=True))
    return h.orchestrator().run(EnumFloorFillCommand.TICK).decision_projection()


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The orchestrator's consumer: run the named case, answer its projection."""
    return {"id": payload["id"], "output": run_case(payload["case"])}


def in_process() -> dict[str, str]:
    return {name: json.dumps(run_case(name), sort_keys=True) for name in CASES}


class Bus:
    """Carry one JSON request to the reducer's consumer and the JSON output back."""

    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="floor-fill-parity", group="parity")
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.prefix = uuid.uuid4().hex

    async def start(self) -> None:
        await self.bus.start()

        async def serve(message: Any) -> None:
            reply = decide(json.loads(message.value))
            await self.bus.publish(
                f"{self.prefix}.res", None, json.dumps(reply).encode()
            )

        async def answer(message: Any) -> None:
            reply = json.loads(message.value)
            self.waiting.pop(reply["id"]).set_result(reply)

        await self.bus.subscribe(
            f"{self.prefix}.req", on_message=serve, group_id="serve"
        )
        await self.bus.subscribe(
            f"{self.prefix}.res", on_message=answer, group_id="ask"
        )

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.bus.publish(f"{self.prefix}.req", None, json.dumps(payload).encode())
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaBus(Bus):
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.prefix = f"floor-fill-parity-{uuid.uuid4().hex}"
        self.tasks: list[asyncio.Task[None]] = []
        self.consumers: list[Any] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, security_protocol="PLAINTEXT"
        )
        await self.producer.start()

        async def consume(topic: str, handle: Callable[[dict[str, Any]], Any]) -> None:
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
                f"{self.prefix}.res", json.dumps(decide(request)).encode()
            )

        async def answer(reply: dict[str, Any]) -> None:
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        self.tasks.append(asyncio.create_task(consume(f"{self.prefix}.req", serve)))
        self.tasks.append(asyncio.create_task(consume(f"{self.prefix}.res", answer)))
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.producer.send_and_wait(
            f"{self.prefix}.req", json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for consumer in self.consumers:
            await consumer.stop()
        await self.producer.stop()


async def run_over(bus: Bus, profile: str, binding: str) -> ModelScenarioRun:
    await bus.start()
    try:
        rows = []
        for name in CASES:
            reply = await bus.call({"id": uuid.uuid4().hex, "case": name})
            rows.append(
                ModelScenarioRow(
                    key=name, content=json.dumps(reply["output"], sort_keys=True)
                )
            )
    finally:
        await bus.stop()
    return ModelScenarioRun(
        profile_id=profile,
        binding=binding,
        proof_eligible=True,
        verdicts=("PASS",),
        rows=tuple(rows),
        counts={"decisions": len(rows)},
    )


def make_bus(binding: str) -> Bus:
    if binding == "inmemory":
        return InmemoryBus()
    return RedpandaBus(os.environ[BOOTSTRAP_ENV])


def parity_run(*, profiles: tuple[str, ...]) -> tuple[ModelScenarioRun, ...]:
    chosen = ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [p.model_dump() for p in OVERLAY.profiles if p.id in profiles],
            "bindings": {p: OVERLAY.bindings[p] for p in profiles},
            "proof_bindings": list(OVERLAY.proof_bindings),
        }
    )
    return tuple(
        asyncio.run(
            run_over(
                make_bus(chosen.binding_id(profile.id, "BUS")),
                profile.id,
                chosen.binding_id(profile.id, "BUS"),
            )
        )
        for profile in chosen.profiles
    )


def test_floor_fill_orchestrator_parity_the_scenario_decides_what_the_rules_say() -> (
    None
):
    rows = {name: json.loads(row) for name, row in in_process().items()}
    four = rows["00:four-lines"]
    assert [r[1] for r in four["key_results"]] == ["started"] * 4
    assert four["status"] == "OK"
    assert four["exit_code"] == 0
    refusing = rows["01:one-refusing-runner"]
    assert tuple(refusing["key_results"][1][:2]) == ("floor:bravo:b1", "failed")
    assert any(
        line.startswith("MISSING line-stopped:bravo") for line in refusing["lines"]
    )
    degraded = rows["02:degraded-read"]
    assert degraded["status"] == "DEGRADED"
    assert degraded["exit_code"] == 3
    assert degraded["lines"] == ["UNKNOWN pool-unread:no host answered"]
    assert degraded["key_results"] == []


def test_floor_fill_orchestrator_parity_in_memory_bus_matches_the_in_process_decisions() -> (
    None
):
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_floor_fill_orchestrator_parity_redpanda_overlay_matches_the_in_memory_overlay() -> (
    None
):
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(CASES)
