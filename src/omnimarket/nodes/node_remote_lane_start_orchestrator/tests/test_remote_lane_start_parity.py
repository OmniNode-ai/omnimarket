# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start decides the same on the in-memory bus and on a Redpanda bus (OMN-22848, plan T15 AC7).

Four scenarios (placed and the engine ends declaring a PR, no host, CLAIM refused, engine fails)
each go as a JSON message on the bus under test; the consumer runs the orchestrator over the
scripted transport and a ledger fixture file and publishes its decision projection back: the
outcome, the exit code, the printed lines, the ordered host and ledger calls and the CLAIM and
TERMINAL cells (time and run id excluded). ``compare_runs`` judges the in-memory run against the
Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names in
``FLOOR_FILL_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the proof):

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -k remote_lane_start -q
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
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
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    Rig,
    cells,
    request,
    snapshot,
    stream_result,
)

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


def golden_scenario() -> list[tuple[str, dict[str, Any]]]:
    done = {"exit_polls": 1, "log_lines": ["starting", stream_result(DONE_MESSAGE)]}
    return [
        ("00:placed", {"script": done, "slots": 2, "refuse": False}),
        ("01:no-host", {"script": done, "slots": 0, "refuse": False}),
        ("02:claim-refused", {"script": done, "slots": 2, "refuse": True}),
        (
            "03:engine-fails",
            {
                "script": {"exit_code": 1, "log_lines": ["boom"]},
                "slots": 2,
                "refuse": False,
            },
        ),
    ]


def project(scenario: dict[str, Any]) -> dict[str, Any]:
    """Run one scenario through the orchestrator and return its decision projection."""
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(
            Path(tmp),
            ModelScriptedTransportScript.model_validate(scenario["script"]),
            pool=snapshot(slots=scenario["slots"]),
            refuse_ledger=scenario["refuse"],
        )
        result = rig.orchestrator.run(request())
        rows = {
            kind: [cells(r) for r in rig.ledger_rows(kind)]
            for kind in ("CLAIM", "TERMINAL", "STATUS")
        }
        return {
            "outcome": result.outcome.value,
            "exit_code": result.exit_code,
            "lines": list(result.lines),
            "calls": rig.calls,
            "rows": rows,
            "receipt_state": result.receipt.state,
        }


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": payload["id"], "output": project(payload["request"])}


def in_process() -> dict[str, str]:
    return {
        name: json.dumps(project(req), sort_keys=True)
        for name, req in golden_scenario()
    }


class Bus:
    """Carry one JSON request to the reducer's consumer and the JSON output back."""

    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(
            environment="remote-lane-start-parity", group="parity"
        )
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
        self.prefix = f"remote-lane-start-parity-{uuid.uuid4().hex}"
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
        for name, req in golden_scenario():
            reply = await bus.call({"id": uuid.uuid4().hex, "request": req})
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


def test_remote_lane_start_parity_the_scenario_decides_what_the_sequence_says() -> None:
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert rows["00:placed"]["outcome"] == "ended"
    assert rows["00:placed"]["exit_code"] == 0
    assert rows["00:placed"]["rows"]["TERMINAL"][0]["outcome"] == "handed-off"
    assert rows["01:no-host"]["outcome"] == "no-host"
    assert rows["01:no-host"]["calls"] == []
    assert rows["02:claim-refused"]["exit_code"] == 70
    assert not [c for c in rows["02:claim-refused"]["calls"] if c.startswith("host:")]
    assert rows["03:engine-fails"]["rows"]["TERMINAL"][0]["outcome"] == "failed"


def test_remote_lane_start_parity_in_memory_bus_matches_the_in_process_decisions() -> (
    None
):
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_remote_lane_start_parity_redpanda_overlay_matches_the_in_memory_overlay() -> (
    None
):
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(golden_scenario())
