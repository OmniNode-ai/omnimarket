# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_process_effect decides the same on the in-memory bus and on a Redpanda bus (G-NODE point 5).

Each scenario's request and the results its scripted runner answers travel as a JSON message on the bus under test;
the node's consumer rebuilds both, runs the effect with the scripted adapter and publishes the outcome as a JSON message
back. The outcome of every scenario is one row of the run, and the parity run's own comparison (``compare_runs``) judges
the in-memory run against the Redpanda run. The real posix adapter is proven by its own tests and by the lab step.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names in
``FLOOR_FILL_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the proof, never the dev lane's):

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -k lane_process -q
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
from omnibase_spi.contracts.services import (
    ModelLaneProcessRunResult,
    ModelLaneProcessStartResult,
    ModelLaneStartSpec,
)

from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelScenarioRow,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_lane_process_effect.adapters import HandlerScriptedRunner
from omnimarket.nodes.node_lane_process_effect.models import (
    EnumLaneProcessOperation,
    ModelLaneProcessRequest,
)
from omnimarket.nodes.node_lane_process_effect.tests.support import handler_with

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

SPEC = ModelLaneStartSpec(
    argv=("example-entry", "--lane", "lane-1"),
    environment=(("EXAMPLE_KEY", "value"),),
    working_directory="/example/work",
    lane="lane-1",
)
SCRIPT_RUNNER_SPEC = ModelLaneStartSpec(
    argv=("example", "onex_remote_lane.py"),
    working_directory="/example/work",
    lane="lane-1",
)


def start_request(spec: ModelLaneStartSpec = SPEC) -> dict[str, Any]:
    return ModelLaneProcessRequest(
        operation=EnumLaneProcessOperation.START_DETACHED, spec=spec
    ).model_dump(mode="json")


def run_request(spec: ModelLaneStartSpec = SPEC) -> dict[str, Any]:
    return ModelLaneProcessRequest(
        operation=EnumLaneProcessOperation.RUN_BOUNDED, spec=spec, timeout_seconds=30.0
    ).model_dump(mode="json")


def scenarios() -> dict[str, dict[str, Any]]:
    """Each operation and each outcome, with the results the scripted runner answers for it."""
    started = ModelLaneProcessStartResult(state="started", pid=4242).model_dump(
        mode="json"
    )
    start_failed = ModelLaneProcessStartResult(
        state="failed", cause="working directory missing: /example/work"
    ).model_dump(mode="json")
    exited = ModelLaneProcessRunResult(
        state="exited", exit_code=3, stdout="out"
    ).model_dump(mode="json")
    timed_out = ModelLaneProcessRunResult(state="timed_out").model_dump(mode="json")
    run_failed = ModelLaneProcessRunResult(
        state="failed", cause="executable missing: example-entry"
    ).model_dump(mode="json")
    return {
        "lane_process:start-started": {"request": start_request(), "start": started},
        "lane_process:start-failed": {
            "request": start_request(),
            "start": start_failed,
        },
        "lane_process:run-exited": {"request": run_request(), "run": exited},
        "lane_process:run-timed-out": {"request": run_request(), "run": timed_out},
        "lane_process:run-failed": {"request": run_request(), "run": run_failed},
        "lane_process:start-refused-script-runner": {
            "request": start_request(SCRIPT_RUNNER_SPEC),
            "start": started,
        },
        "lane_process:run-refused-script-runner": {
            "request": run_request(SCRIPT_RUNNER_SPEC),
            "run": exited,
        },
    }


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    scripted = payload["scenario"]
    runner = HandlerScriptedRunner(
        starts=(ModelLaneProcessStartResult.model_validate(scripted["start"]),)
        if "start" in scripted
        else (),
        runs=(ModelLaneProcessRunResult.model_validate(scripted["run"]),)
        if "run" in scripted
        else (),
    )
    result = handler_with(runner).handle(
        ModelLaneProcessRequest.model_validate(scripted["request"])
    )
    return {
        "id": payload["id"],
        "decision": {
            "result": result.model_dump(mode="json"),
            "calls": [name for name, _, _ in runner.calls],
        },
    }


def in_process() -> dict[str, str]:
    return {
        name: json.dumps(
            decide({"id": "x", "scenario": scenario})["decision"], sort_keys=True
        )
        for name, scenario in scenarios().items()
    }


class Bus:
    """Carry one JSON request to the node's consumer and the JSON decision back."""

    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="lane-process-parity", group="parity")
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
        self.prefix = f"lane-process-parity-{uuid.uuid4().hex}"
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

        async def serve(request_payload: dict[str, Any]) -> None:
            await self.producer.send_and_wait(
                f"{self.prefix}.res", json.dumps(decide(request_payload)).encode()
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
        for name, scenario in scenarios().items():
            payload = {"id": uuid.uuid4().hex, "scenario": scenario}
            reply = await bus.call(payload)
            rows.append(
                ModelScenarioRow(
                    key=name, content=json.dumps(reply["decision"], sort_keys=True)
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


def test_lane_process_parity_scenarios_decide_what_the_node_says() -> None:
    decisions = {k: json.loads(v) for k, v in in_process().items()}
    outcomes = {k: v["result"]["outcome"] for k, v in decisions.items()}
    assert outcomes == {
        "lane_process:start-started": "started",
        "lane_process:start-failed": "failed",
        "lane_process:run-exited": "exited",
        "lane_process:run-timed-out": "timed_out",
        "lane_process:run-failed": "failed",
        "lane_process:start-refused-script-runner": "refused",
        "lane_process:run-refused-script-runner": "refused",
    }
    # A refused spec reaches no runner: the scripted runner records no call for it.
    assert decisions["lane_process:start-refused-script-runner"]["calls"] == []
    assert decisions["lane_process:run-refused-script-runner"]["calls"] == []
    assert decisions["lane_process:start-started"]["calls"] == ["start_detached"]
    assert decisions["lane_process:run-exited"]["calls"] == ["run_bounded"]


def test_lane_process_parity_in_memory_bus_matches_the_in_process_decisions() -> None:
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_lane_process_parity_redpanda_overlay_matches_the_in_memory_overlay() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(scenarios())
